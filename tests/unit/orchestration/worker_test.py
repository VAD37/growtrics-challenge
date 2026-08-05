"""The claim loop.

The three behaviours that matter when a process dies: a clean finish removes the queue row, an
abandoned claim is never handed back (so the lease has to expire before anyone else can have it),
and a stop signal drains the job in hand instead of dropping it.

`asyncio.sleep` is injected so the loop can be driven at full speed. Nothing here waits on wall
clock time; the lease deadlines come from a `FrozenClock` a test moves by hand.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from fakes_test import (
    JOB_ID,
    LEASE_SECONDS,
    OWNER,
    T0,
    WORK_ITEM_ID,
    FakeWorkQueue,
    FrozenClock,
    make_job,
)

from app.domain.records import ClaimedWorkItem, JobRecord
from app.orchestration.engine.policy import heartbeat_interval
from app.orchestration.ports import QueuedWorkItem
from app.worker import WorkerConfig, WorkerLoop, heartbeat_lease


@dataclass(slots=True)
class RecordingSleep:
    """A stand-in for `asyncio.sleep` that records what it was asked to wait for.

    It still yields to the event loop, so a task waiting on it is descheduled exactly as it
    would be under the real sleep. It simply never waits.
    """

    intervals: list[float] = field(default_factory=list)
    on_call: Callable[[int], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)
        if self.on_call is not None:
            self.on_call(len(self.intervals))
        await asyncio.sleep(0)


@dataclass(slots=True)
class FakeRunner:
    """Stands in for `JobRunner`. Records what it was handed and can be told to blow up."""

    result: JobRecord | None = None
    raises: Exception | None = None
    seen: list[ClaimedWorkItem] = field(default_factory=list)
    on_run: Callable[[], None] | None = None
    wait_for: asyncio.Event | None = None

    async def run(self, item: ClaimedWorkItem) -> JobRecord | None:
        self.seen.append(item)
        if self.on_run is not None:
            self.on_run()
        if self.wait_for is not None:
            await self.wait_for.wait()
        if self.raises is not None:
            raise self.raises
        return self.result


def queued_item() -> QueuedWorkItem:
    return QueuedWorkItem(item_id=WORK_ITEM_ID, job_id=JOB_ID, available_at=T0, created_at=T0)


def build(
    *,
    queue: FakeWorkQueue,
    runner: FakeRunner,
    stop: asyncio.Event,
    sleep: RecordingSleep,
    clock: FrozenClock,
) -> WorkerLoop:
    return WorkerLoop(
        queue=queue,
        runner=runner,
        clock=clock,
        config=WorkerConfig(
            owner=OWNER,
            lease_seconds=LEASE_SECONDS,
            poll_seconds=2.0,
            max_concurrent_jobs=1,
        ),
        stop=stop,
        sleep=sleep,
    )


# --------------------------------------------------------------------------- the loop


async def test_a_finished_job_takes_its_queue_row_with_it() -> None:
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    stop = asyncio.Event()
    runner = FakeRunner(result=make_job(), on_run=stop.set)

    await build(
        queue=queue, runner=runner, stop=stop, sleep=RecordingSleep(), clock=clock
    ).run_forever()

    assert [item.item_id for item in runner.seen] == [WORK_ITEM_ID]
    assert queue.completed == [WORK_ITEM_ID]
    assert queue.released == []


async def test_a_crashing_run_leaves_the_claim_where_it_is() -> None:
    """An abandoned lease is the only signal the deferred reclaim will ever have.

    Handing the row straight back would re-run a job the demo has already failed, and would hide
    the one condition the timeout system is supposed to notice.
    """
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    stop = asyncio.Event()
    runner = FakeRunner(raises=RuntimeError("the adapter fell over"), on_run=stop.set)

    await build(
        queue=queue, runner=runner, stop=stop, sleep=RecordingSleep(), clock=clock
    ).run_forever()

    assert queue.completed == []
    assert queue.released == []
    assert WORK_ITEM_ID in queue.claimed


async def test_an_empty_queue_waits_for_the_poll_interval() -> None:
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    stop = asyncio.Event()
    sleep = RecordingSleep(on_call=lambda _: stop.set())

    await build(queue=queue, runner=FakeRunner(), stop=stop, sleep=sleep, clock=clock).run_forever()

    assert sleep.intervals == [2.0]


async def test_a_stop_between_claiming_and_running_hands_the_row_straight_back() -> None:
    """The one clean release: nothing has started, so the next worker should not wait a lease."""
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    stop = asyncio.Event()
    stop.set()
    runner = FakeRunner()

    loop = build(queue=queue, runner=runner, stop=stop, sleep=RecordingSleep(), clock=clock)
    await loop.claim_and_run_once()

    assert runner.seen == []
    assert queue.released == [WORK_ITEM_ID]
    assert queue.completed == []


async def test_a_stop_signal_drains_the_job_in_hand() -> None:
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    queue.seed(
        QueuedWorkItem(
            item_id="wi_" + "1" * 26, job_id="job_" + "1" * 26, available_at=T0, created_at=T0
        )
    )
    stop = asyncio.Event()
    runner = FakeRunner(result=make_job(), on_run=stop.set)

    await build(
        queue=queue, runner=runner, stop=stop, sleep=RecordingSleep(), clock=clock
    ).run_forever()

    assert len(runner.seen) == 1
    assert queue.completed == [WORK_ITEM_ID]
    assert len(queue.available) == 1


async def test_the_demo_worker_takes_one_job_at_a_time() -> None:
    """`work_max_concurrent_jobs` is 1 and the loop is single-flight by construction."""
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    stop = asyncio.Event()
    in_flight = 0
    peak = 0

    def enter() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        in_flight -= 1
        stop.set()

    runner = FakeRunner(result=make_job(), on_run=enter)
    await build(
        queue=queue, runner=runner, stop=stop, sleep=RecordingSleep(), clock=clock
    ).run_forever()

    assert peak == 1


# --------------------------------------------------------------------------- the lease


async def test_the_heartbeat_renews_a_third_of_a_lease_at_a_time() -> None:
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    item = await queue.claim(OWNER, LEASE_SECONDS)
    assert item is not None
    assert item.claimed_until == T0 + timedelta(seconds=LEASE_SECONDS)

    stop = asyncio.Event()
    interval = heartbeat_interval(LEASE_SECONDS)

    def beat(count: int) -> None:
        clock.advance(interval)
        if count == 3:
            stop.set()

    sleep = RecordingSleep(on_call=beat)
    await heartbeat_lease(
        queue=queue,
        item=item,
        owner=OWNER,
        lease_seconds=LEASE_SECONDS,
        stop=stop,
        sleep=sleep,
    )

    assert sleep.intervals == [20.0, 20.0, 20.0]
    assert [deadline for _, deadline in queue.heartbeats] == [
        T0 + timedelta(seconds=80),
        T0 + timedelta(seconds=100),
    ]
    assert queue.claimed[WORK_ITEM_ID].claimed_until == T0 + timedelta(seconds=100)


async def test_a_lost_lease_stops_the_heartbeat() -> None:
    """Somebody else holds the row now. Beating on would overwrite their claim."""
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock, heartbeat_returns_none_after=0)
    queue.seed(queued_item())
    item = await queue.claim(OWNER, LEASE_SECONDS)
    assert item is not None

    sleep = RecordingSleep()
    await heartbeat_lease(
        queue=queue,
        item=item,
        owner=OWNER,
        lease_seconds=LEASE_SECONDS,
        stop=asyncio.Event(),
        sleep=sleep,
    )

    assert sleep.intervals == [20.0]
    assert queue.heartbeats == []


async def test_a_job_in_flight_is_heartbeaten_while_it_runs() -> None:
    """The lease is held for as long as the work takes, not for as long as one lease lasts."""
    clock = FrozenClock()
    queue = FakeWorkQueue(clock=clock)
    queue.seed(queued_item())
    stop = asyncio.Event()
    resume = asyncio.Event()

    def on_sleep(count: int) -> None:
        if count >= 2:
            resume.set()  # one whole beat has landed; let the job finish
            stop.set()

    runner = FakeRunner(result=make_job(), wait_for=resume)
    await build(
        queue=queue, runner=runner, stop=stop, sleep=RecordingSleep(on_call=on_sleep), clock=clock
    ).run_forever()

    assert len(queue.heartbeats) >= 1
    assert queue.completed == [WORK_ITEM_ID]
