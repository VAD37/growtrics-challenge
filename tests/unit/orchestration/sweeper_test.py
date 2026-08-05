"""The two timeouts and the ticker that runs them.

Two failures wear one code and they are not the same event, so each one is pinned on its own: a
job nobody ever claimed, and a claim nobody ever finished. The boundary cases are the point --
a sweep that is a second too eager fails jobs that were about to run, and one that is a second
too slow is a queue that grows.

Every threshold here comes from a `SweepThresholds` a test built, and the last test in the file
is the one that says those defaults are the settings.
"""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fakes_test import (
    JOB_ID,
    LEASE_SECONDS,
    OWNER,
    T0,
    WORK_ITEM_ID,
    FakeJobRepository,
    FakeWorkQueue,
    FrozenClock,
    make_job,
)

from app.config import settings
from app.domain.enums import JobStatus, StageName
from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode
from app.domain.ids import (
    JobId,
    derive_job_id,
    derive_trace_id,
    derive_work_item_id,
    mint_request_key,
)
from app.domain.records import ClaimedWorkItem, JobRecord
from app.orchestration.engine.policy import classify_failure
from app.orchestration.engine.sweeper import (
    DEFAULT_THRESHOLDS,
    SWEEP_BATCH_LIMIT,
    SweepThresholds,
    fail_pending_jobs,
    fail_stale_jobs,
    reclaim_expired_leases,
    run_sweeper,
    sweep_once,
)
from app.orchestration.ports import QueuedWorkItem
from app.worker import WorkerConfig, WorkerLoop, run_with_sweep

PENDING_MAX: int = 3600
STALE_AFTER: int = 900
MAX_CLAIMS: int = 3

THRESHOLDS: SweepThresholds = SweepThresholds(
    pending_max_seconds=PENDING_MAX,
    stale_after_seconds=STALE_AFTER,
    max_claims=MAX_CLAIMS,
    batch_limit=SWEEP_BATCH_LIMIT,
)


def other_job_id(seed: int) -> JobId:
    return derive_job_id(mint_request_key(bytes([seed]) * 16))


@dataclass(slots=True)
class RecordingSleep:
    """`asyncio.sleep` that records what it was asked to wait for and never waits.

    It still yields to the event loop, so a task waiting on it is descheduled exactly as it would
    be under the real sleep.
    """

    intervals: list[float] = field(default_factory=list)
    on_call: Callable[[int], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)
        if self.on_call is not None:
            self.on_call(len(self.intervals))
        await asyncio.sleep(0)


def build(*, at: datetime = T0) -> tuple[FakeJobRepository, FakeWorkQueue, FrozenClock]:
    """A repository and a queue that share one clock, as the worker's adapters share one."""
    clock = FrozenClock(at=at)
    return FakeJobRepository(clock=clock), FakeWorkQueue(clock=clock), clock


def seed_queued(
    jobs: FakeJobRepository, queue: FakeWorkQueue, *, waited: int, job_id: JobId = JOB_ID
) -> JobRecord:
    """A job submitted `waited` seconds ago that no worker has claimed, plus its queue row."""
    submitted = T0 - timedelta(seconds=waited)
    job = jobs.seed(make_job(job_id=job_id, status=JobStatus.QUEUED, created_at=submitted))
    queue.seed(
        QueuedWorkItem(
            item_id=derive_work_item_id(job_id),
            job_id=job_id,
            available_at=submitted,
            created_at=submitted,
        )
    )
    return job


def seed_running(
    jobs: FakeJobRepository,
    queue: FakeWorkQueue,
    *,
    untouched_for: int,
    lease_left: int,
    claims: int = 0,
    job_id: JobId = JOB_ID,
) -> JobRecord:
    """A job a worker took: `RUNNING`, with a claimed row whose lease has `lease_left` to go."""
    job = jobs.seed(
        make_job(
            job_id=job_id,
            status=JobStatus.RUNNING,
            stage=StageName.GENERATING,
            created_at=T0 - timedelta(seconds=untouched_for),
        )
    )
    queue.seed_claimed(
        ClaimedWorkItem(
            item_id=derive_work_item_id(job_id),
            job_id=job_id,
            claimed_by=OWNER,
            claimed_until=T0 + timedelta(seconds=lease_left),
            claim_count=claims,
        )
    )
    return job


# --------------------------------------------------------------------------- pending too long


async def test_a_job_still_inside_the_wait_is_left_alone() -> None:
    """One second short of the threshold. The queue is slow, not broken."""
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=PENDING_MAX - 1)

    assert await fail_pending_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    assert jobs.transitions == []
    assert jobs.rows[JOB_ID].status is JobStatus.QUEUED
    assert len(queue.available) == 1


async def test_a_job_that_has_waited_the_whole_threshold_is_failed() -> None:
    """The other side of the same second. Inclusive at the edge, as leases are."""
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=PENDING_MAX)

    assert await fail_pending_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 1
    failed = jobs.rows[JOB_ID]
    assert failed.status is JobStatus.FAILED
    assert failed.stage is StageName.FAILED
    assert failed.failure is not None
    assert failed.failure.code is ErrorCode.JOB_TIMED_OUT


async def test_failing_a_pending_job_takes_its_queue_row_with_it() -> None:
    """Otherwise a worker claims a FAILED job an hour later and the runner drops it, forever."""
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=PENDING_MAX * 2)

    await fail_pending_jobs(jobs, queue, clock, thresholds=THRESHOLDS)

    assert queue.discarded == [derive_work_item_id(JOB_ID)]
    assert queue.available == []
    assert await queue.claim(OWNER, LEASE_SECONDS) is None


async def test_the_wait_is_measured_against_the_threshold_it_was_given() -> None:
    """The number is a setting, not a constant in the sweep body. Move it, the verdict moves."""
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=120)

    assert await fail_pending_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    patient = SweepThresholds(pending_max_seconds=60)
    assert await fail_pending_jobs(jobs, queue, clock, thresholds=patient) == 1


async def test_one_tick_reads_no_more_of_the_backlog_than_it_was_allowed() -> None:
    """The 50k case. A sweep that read the whole queue would trade one unbounded thing for two."""
    jobs, queue, clock = build()
    for seed in range(5):
        seed_queued(jobs, queue, waited=PENDING_MAX + seed, job_id=other_job_id(seed))

    capped = SweepThresholds(pending_max_seconds=PENDING_MAX, batch_limit=2)
    assert await fail_pending_jobs(jobs, queue, clock, thresholds=capped) == 2
    assert await fail_pending_jobs(jobs, queue, clock, thresholds=capped) == 2
    assert await fail_pending_jobs(jobs, queue, clock, thresholds=capped) == 1


async def test_the_oldest_waiter_is_failed_first() -> None:
    """A capped sweep has to drain the worst of the backlog rather than a random slice of it."""
    jobs, queue, clock = build()
    for seed in range(3):
        seed_queued(jobs, queue, waited=PENDING_MAX + seed * 100, job_id=other_job_id(seed))

    capped = SweepThresholds(pending_max_seconds=PENDING_MAX, batch_limit=1)
    await fail_pending_jobs(jobs, queue, clock, thresholds=capped)

    assert [transition.job_id for transition in jobs.transitions] == [other_job_id(2)]


# --------------------------------------------------------------------------- lease expired


async def test_a_live_lease_is_never_reclaimed() -> None:
    """A worker holding a lease is a worker still working. Nothing here may take its row."""
    _, queue, clock = build()
    seed_running(FakeJobRepository(), queue, untouched_for=0, lease_left=1)

    assert await reclaim_expired_leases(queue, clock, thresholds=THRESHOLDS) == 0
    assert WORK_ITEM_ID in queue.claimed
    assert queue.available == []


async def test_a_lapsed_lease_goes_back_on_the_queue_with_its_claim_counted() -> None:
    _, queue, clock = build()
    seed_running(FakeJobRepository(), queue, untouched_for=0, lease_left=-1)

    assert await reclaim_expired_leases(queue, clock, thresholds=THRESHOLDS) == 1
    assert WORK_ITEM_ID not in queue.claimed
    assert [item.item_id for item in queue.available] == [WORK_ITEM_ID]
    assert queue.counts[WORK_ITEM_ID] == 1


async def test_an_item_past_max_claims_is_failed_rather_than_handed_back_forever() -> None:
    """A poison item handed back for the fourth time is a queue that never drains."""
    jobs, queue, clock = build()
    seed_running(jobs, queue, untouched_for=0, lease_left=-1, claims=MAX_CLAIMS)

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 1
    assert jobs.rows[JOB_ID].status is JobStatus.FAILED
    assert queue.discarded == [WORK_ITEM_ID]

    assert await reclaim_expired_leases(queue, clock, thresholds=THRESHOLDS) == 0
    assert queue.available == []


async def test_an_item_one_claim_short_of_the_cap_is_still_handed_back() -> None:
    jobs, queue, clock = build()
    seed_running(jobs, queue, untouched_for=0, lease_left=-1, claims=MAX_CLAIMS - 1)

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    assert await reclaim_expired_leases(queue, clock, thresholds=THRESHOLDS) == 1
    assert jobs.rows[JOB_ID].status is JobStatus.RUNNING


async def test_a_worn_out_item_whose_lease_is_still_live_is_left_alone() -> None:
    """Last claim, still beating. It has not failed yet; it is on its final attempt."""
    jobs, queue, clock = build()
    seed_running(jobs, queue, untouched_for=0, lease_left=LEASE_SECONDS, claims=MAX_CLAIMS)

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    assert jobs.rows[JOB_ID].status is JobStatus.RUNNING
    assert queue.discarded == []


async def test_an_exhausted_row_pointing_at_no_job_is_just_removed() -> None:
    """At-least-once delivery leaves rows behind. Nobody can run it and nobody can fail it."""
    jobs, queue, clock = build()
    seed_running(FakeJobRepository(), queue, untouched_for=0, lease_left=-1, claims=MAX_CLAIMS)

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    assert queue.discarded == [WORK_ITEM_ID]


async def test_a_running_job_that_stopped_moving_is_given_up_on() -> None:
    """`job_stale_seconds`. `updated_at` moves on every stage, so a still row is a hung job."""
    jobs, queue, clock = build()
    seed_running(jobs, queue, untouched_for=STALE_AFTER, lease_left=LEASE_SECONDS)

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 1
    failed = jobs.rows[JOB_ID]
    assert failed.status is JobStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.code is ErrorCode.JOB_TIMED_OUT
    assert queue.discarded == [WORK_ITEM_ID]


async def test_a_running_job_inside_its_stale_window_is_left_alone() -> None:
    jobs, queue, clock = build()
    seed_running(jobs, queue, untouched_for=STALE_AFTER - 1, lease_left=LEASE_SECONDS)

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    assert jobs.rows[JOB_ID].status is JobStatus.RUNNING


# --------------------------------------------------------------------------- terminal jobs


async def test_neither_sweep_touches_a_job_that_has_already_finished() -> None:
    """Invariant 4 of `VideoJob`: nothing leaves a terminal status, however old the row is."""
    jobs, queue, clock = build()
    for seed, status in enumerate((JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)):
        jobs.seed(
            make_job(
                job_id=other_job_id(seed),
                status=status,
                stage=StageName.DONE,
                created_at=T0 - timedelta(days=30),
            )
        )

    report = await sweep_once(queue, jobs, clock, thresholds=THRESHOLDS)

    assert report.touched == 0
    assert jobs.transitions == []
    assert queue.discarded == []


async def test_a_terminal_job_holding_a_worn_out_row_loses_the_row_and_keeps_its_status() -> None:
    """The row still has to go, or a worker claims a SUCCEEDED job and the runner drops it."""
    jobs, queue, clock = build()
    jobs.seed(
        make_job(
            status=JobStatus.SUCCEEDED, stage=StageName.DONE, created_at=T0 - timedelta(days=1)
        )
    )
    queue.seed_claimed(
        ClaimedWorkItem(
            item_id=WORK_ITEM_ID,
            job_id=JOB_ID,
            claimed_by=OWNER,
            claimed_until=T0 - timedelta(seconds=1),
            claim_count=MAX_CLAIMS,
        )
    )

    assert await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS) == 0
    assert jobs.rows[JOB_ID].status is JobStatus.SUCCEEDED
    assert jobs.transitions == []
    assert queue.discarded == [WORK_ITEM_ID]


# --------------------------------------------------------------------------- the failure written


async def test_the_failure_says_where_the_job_died_and_reads_from_the_catalog() -> None:
    """D062: the words come from `ERROR_CATALOG`, never from the raise site."""
    jobs, queue, clock = build()
    job = seed_running(jobs, queue, untouched_for=STALE_AFTER, lease_left=LEASE_SECONDS)

    await fail_stale_jobs(jobs, queue, clock, thresholds=THRESHOLDS)

    failure = jobs.rows[JOB_ID].failure
    assert failure is not None
    assert failure.message == ERROR_CATALOG[ErrorCode.JOB_TIMED_OUT].message
    assert failure.stage is StageName.GENERATING
    assert failure.retryable is False
    assert failure.occurred_at == T0
    assert failure.trace_id == derive_trace_id(JOB_ID, job.attempt)
    assert jobs.rows[JOB_ID].progress_percent == job.progress_percent


def test_the_timeout_code_is_a_job_failure_and_never_a_response_status() -> None:
    """`http_status is None` is how `classify_failure` tells the two apart, so it has to be None."""
    assert ERROR_CATALOG[ErrorCode.JOB_TIMED_OUT].http_status is None
    assert classify_failure(DomainError(ErrorCode.JOB_TIMED_OUT)).code is ErrorCode.JOB_TIMED_OUT


# --------------------------------------------------------------------------- one whole tick


async def test_a_second_identical_tick_finds_nothing_left_to_do() -> None:
    """Idempotence, which for a sweep means never failing the same job twice."""
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=PENDING_MAX, job_id=other_job_id(1))
    seed_running(jobs, queue, untouched_for=STALE_AFTER, lease_left=-1, job_id=other_job_id(2))

    first = await sweep_once(queue, jobs, clock, thresholds=THRESHOLDS)
    written = len(jobs.transitions)
    second = await sweep_once(queue, jobs, clock, thresholds=THRESHOLDS)

    assert (first.pending_failed, first.stale_failed) == (1, 1)
    assert second.touched == 0
    assert len(jobs.transitions) == written


async def test_a_tick_fails_the_abandoned_before_it_hands_anything_back() -> None:
    """Reclaiming first would make a row claimable that the same tick is about to fail."""
    jobs, queue, clock = build()
    seed_running(jobs, queue, untouched_for=STALE_AFTER, lease_left=-1)

    report = await sweep_once(queue, jobs, clock, thresholds=THRESHOLDS)

    assert (report.stale_failed, report.leases_reclaimed) == (1, 0)
    assert queue.available == []


# --------------------------------------------------------------------------- the ticker


async def test_the_ticker_keeps_going_after_a_tick_raises() -> None:
    """A database blip in a background scan must not end the sweep for the life of the process."""
    jobs, queue, clock = build()
    queue.sweep_error = RuntimeError("the database went away")
    stop = asyncio.Event()
    sleep = RecordingSleep(on_call=lambda count: stop.set() if count == 3 else None)

    await run_sweeper(queue, jobs, clock, stop=stop, interval_seconds=30.0, sleep=sleep)

    assert sleep.intervals == [30.0, 30.0, 30.0]


async def test_a_stop_already_set_means_the_ticker_never_sweeps_at_all() -> None:
    """The signal is checked before the first tick, so a shutdown mid-start sweeps nothing."""
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=PENDING_MAX * 10)
    stop = asyncio.Event()
    stop.set()
    sleep = RecordingSleep()

    await run_sweeper(queue, jobs, clock, stop=stop, thresholds=THRESHOLDS, sleep=sleep)

    assert sleep.intervals == []
    assert jobs.transitions == []
    assert jobs.rows[JOB_ID].status is JobStatus.QUEUED


async def test_the_ticker_sweeps_on_every_tick() -> None:
    jobs, queue, clock = build()
    seed_queued(jobs, queue, waited=PENDING_MAX, job_id=other_job_id(1))
    stop = asyncio.Event()
    ticks = 0

    def count(call: int) -> None:
        nonlocal ticks
        ticks = call
        if call == 2:
            stop.set()

    await run_sweeper(
        queue,
        jobs,
        clock,
        stop=stop,
        thresholds=THRESHOLDS,
        sleep=RecordingSleep(on_call=count),
    )

    assert ticks == 2
    assert jobs.rows[other_job_id(1)].status is JobStatus.FAILED


# --------------------------------------------------------------------------- beside the worker


@dataclass(slots=True)
class OneJobRunner:
    """Stands in for `JobRunner`: holds the job until the sweep has had its turn, then stops."""

    stop: asyncio.Event
    swept: asyncio.Event
    seen: list[ClaimedWorkItem] = field(default_factory=list)

    async def run(self, item: ClaimedWorkItem) -> JobRecord | None:
        self.seen.append(item)
        await self.swept.wait()
        self.stop.set()
        return None


async def test_a_sweep_that_raises_every_tick_does_not_stop_the_worker_claiming() -> None:
    """The two tasks share a process and a stop signal, and nothing else.

    The thing that fails jobs is less important than the thing that finishes them, so a sweep
    that cannot reach the database has to fail quietly beside a worker that still can. The
    runner waits for one failed tick before finishing, so a green run here is a raising sweep
    and a completed job in the same process.
    """
    jobs, queue, clock = build()
    queue.sweep_error = RuntimeError("the sweep cannot read the queue")
    queue.seed(QueuedWorkItem(item_id=WORK_ITEM_ID, job_id=JOB_ID, available_at=T0, created_at=T0))
    stop = asyncio.Event()
    swept = asyncio.Event()
    sweep_sleep = RecordingSleep(on_call=lambda _: swept.set())
    runner = OneJobRunner(stop=stop, swept=swept)
    worker = WorkerLoop(
        queue=queue,
        runner=runner,
        clock=clock,
        config=WorkerConfig(
            owner=OWNER, lease_seconds=LEASE_SECONDS, poll_seconds=2.0, max_concurrent_jobs=1
        ),
        stop=stop,
        sleep=RecordingSleep(),
    )

    await run_with_sweep(
        worker,
        run_sweeper(queue, jobs, clock, stop=stop, sleep=sweep_sleep),
    )

    assert sweep_sleep.intervals != []
    assert [item.item_id for item in runner.seen] == [WORK_ITEM_ID]
    assert queue.completed == [WORK_ITEM_ID]


# --------------------------------------------------------------------------- the numbers


def test_every_threshold_defaults_to_its_setting() -> None:
    """No literal in a sweep body. Change the environment and the sweep changes with it."""
    assert DEFAULT_THRESHOLDS.pending_max_seconds == settings.job_pending_max_seconds
    assert DEFAULT_THRESHOLDS.stale_after_seconds == settings.job_stale_seconds
    assert DEFAULT_THRESHOLDS.max_claims == settings.work_max_claims
    assert DEFAULT_THRESHOLDS.batch_limit == SWEEP_BATCH_LIMIT


def test_the_settings_carry_the_numbers_the_owner_asked_for() -> None:
    assert settings.job_pending_max_seconds == 3600
    assert settings.sweep_interval_seconds == 60.0


def test_the_ticker_defaults_to_the_configured_interval() -> None:
    interval = inspect.signature(run_sweeper).parameters["interval_seconds"]
    assert interval.default == settings.sweep_interval_seconds
