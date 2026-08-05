"""Worker entrypoint: claim one work item, run it, complete it, repeat (D053, D095).

Same image as the API, different role. `python -m app.worker`.

One job at a time. `config.work_max_concurrent_jobs` is 1 because the demo wants a visibly slow
worker, and the loop is single-flight by construction rather than by a semaphore: it awaits the
run before it claims again.

The lease discipline is the part worth reading twice.

- A claim takes `config.work_lease_seconds` and a background task renews it at a third of that
  (`engine/policy.heartbeat_interval`), so a long render keeps its claim without a long lease.
- A clean finish calls `complete`, which removes the row. The demo never retries (docs/demo.md
  D2).
- A **stop between claiming and starting** calls `release`, so a shutdown does not park a job for
  a whole lease. That is the only path that releases.
- **A crash never releases.** An abandoned lease has to expire so the sweep can find it
  (`app/orchestration/engine/sweeper.py`).
- SIGTERM and SIGINT stop the claiming and drain the job in hand rather than dropping it.

The sweep runs here too, as its own task on a ticker beside the claim loop (`run_with_sweep`).
This is the process that has one: a FastAPI handler dies with its request, and a backlog that is
only swept while somebody is polling is not swept. The two tasks share one `asyncio.Event`, so
one SIGTERM drains the job in hand and stops the ticker behind it.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from app.config import settings
from app.domain.records import ClaimedWorkItem, JobRecord
from app.orchestration.engine.policy import heartbeat_interval
from app.orchestration.ports import Clock, WorkQueue

logger = logging.getLogger(__name__)

type SleepFn = Callable[[float], Awaitable[None]]
"""Injected so a test can drive the loop at full speed. Production passes `asyncio.sleep`."""


class RunsJobs(Protocol):
    """`engine.runner.JobRunner`, structurally.

    Declared here rather than imported so the loop can be tested against a fake runner without
    building the runner's five collaborators.
    """

    async def run(self, item: ClaimedWorkItem) -> JobRecord | None: ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Everything the loop reads from the environment, resolved once at startup."""

    owner: str
    lease_seconds: int
    poll_seconds: float
    max_concurrent_jobs: int


async def heartbeat_lease(
    *,
    queue: WorkQueue,
    item: ClaimedWorkItem,
    owner: str,
    lease_seconds: int,
    stop: asyncio.Event,
    sleep: SleepFn,
) -> None:
    """Renew a claim until told to stop or until the claim is no longer ours.

    Returning on a lost lease matters: another holder means the reclaim path (or an operator) has
    already moved the row, and beating on would silently steal it back.
    """
    interval = heartbeat_interval(lease_seconds)
    while not stop.is_set():
        await sleep(interval)
        if stop.is_set():
            return
        renewed = await queue.heartbeat(item.item_id, owner, lease_seconds)
        if renewed is None:
            logger.warning("lost the lease on work item %s; stopping the heartbeat", item.item_id)
            return


class WorkerLoop:
    """Claim, run, complete. The whole worker, minus its adapters."""

    def __init__(
        self,
        *,
        queue: WorkQueue,
        runner: RunsJobs,
        clock: Clock,
        config: WorkerConfig,
        stop: asyncio.Event,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if config.max_concurrent_jobs != 1:
            # @TODO concurrency is not built. The loop is single-flight, so a larger value is
            # ignored rather than honoured; running two jobs means running two processes.
            logger.warning(
                "work_max_concurrent_jobs is %s; this loop runs one job at a time",
                config.max_concurrent_jobs,
            )
        self._queue: WorkQueue = queue
        self._runner: RunsJobs = runner
        self._clock: Clock = clock
        self._config: WorkerConfig = config
        self._stop: asyncio.Event = stop
        self._sleep: SleepFn = sleep

    async def run_forever(self) -> None:
        logger.info("worker %s claiming from the queue", self._config.owner)
        while not self._stop.is_set():
            claimed = await self.claim_and_run_once()
            if not claimed:
                await self._wait_to_poll()
        logger.info("worker %s stopped", self._config.owner)

    async def claim_and_run_once(self) -> bool:
        """One turn of the loop. `False` when there was nothing to do."""
        item = await self._queue.claim(self._config.owner, self._config.lease_seconds)
        if item is None:
            return False
        if self._stop.is_set():
            # Asked to stop before the work started. Hand it straight back so the next worker
            # sees it now rather than after a whole lease has run out.
            await self._queue.release(item.item_id, self._config.owner)
            return False

        logger.info("claimed work item %s for job %s", item.item_id, item.job_id)
        try:
            await self._run_under_lease(item)
        except Exception:
            # The runner turns a failed lesson into a FAILED job, so reaching here means the
            # runner or an adapter itself broke. The claim is deliberately left in place: the
            # row must not be handed straight back to be run again, and its expiry is the only
            # trace of what happened. The sweep reads that expiry and hands the row back, or
            # fails the job once it has burned `work_max_claims` workers.
            logger.exception("work item %s left claimed after an unhandled error", item.item_id)
            return True
        await self._queue.complete(item.item_id, self._config.owner)
        logger.info("completed work item %s", item.item_id)
        return True

    async def _run_under_lease(self, item: ClaimedWorkItem) -> None:
        beating = asyncio.Event()
        beat = asyncio.create_task(
            heartbeat_lease(
                queue=self._queue,
                item=item,
                owner=self._config.owner,
                lease_seconds=self._config.lease_seconds,
                stop=beating,
                sleep=self._sleep,
            )
        )
        try:
            await self._runner.run(item)
        finally:
            beating.set()
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

    async def _wait_to_poll(self) -> None:
        """Idle for the poll interval, but wake at once on a stop signal.

        A race rather than a timeout, so the wait goes through the injected sleep. A polling
        worker that only noticed SIGTERM at the end of its nap would add the poll interval to
        every deployment.
        """
        stopping = asyncio.ensure_future(self._stop.wait())
        napping = asyncio.ensure_future(self._sleep(self._config.poll_seconds))
        try:
            await asyncio.wait({stopping, napping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopping, napping):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM and SIGINT stop the claiming; the job in hand is drained, not dropped."""
    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(received, stop.set)
        except NotImplementedError, AttributeError:
            # Windows event loops have no signal handler support. The docker image is Linux;
            # this branch is what keeps `python -m app.worker` usable on a developer's laptop.
            signal.signal(received, lambda *_: stop.set())


async def run_with_sweep(worker: WorkerLoop, sweep: Awaitable[None]) -> None:
    """Run the claim loop and the sweep ticker side by side under one stop signal.

    The sweep is its own task so a slow tick never delays a claim, and the claim loop is the one
    that decides when the process is done: SIGTERM drains the job in hand, and the ticker is
    cancelled behind it because a backlog scan is not worth holding a shutdown open for.

    `run_sweeper` swallows a failing tick itself, so an exception arriving here is the ticker
    rather than the sweep. It is logged and not re-raised: the worker has already drained, and a
    dead sweeper must not turn a clean shutdown into a crash loop.
    """
    sweeping = asyncio.ensure_future(sweep)
    try:
        await worker.run_forever()
    finally:
        sweeping.cancel()
        try:
            await sweeping
        except asyncio.CancelledError:
            logger.info("sweep ticker cancelled on shutdown")
        except Exception:
            logger.exception("the sweep ticker died; the worker drained anyway")


def build_worker(stop: asyncio.Event) -> tuple[WorkerLoop, Awaitable[None]]:
    """@TODO wire the adapters. The composition root owns this and it is not merged yet.

    It needs the SQL work queue (`app/storage/sql/queue.py`), the SQL job and request
    repositories, the intake, generation and custody services, and a real clock, assembled into
    a `JobRunner` exactly as `app/main.py` assembles the API side, plus a `run_sweeper(...)`
    coroutine over the same queue, job repository, clock and `stop`. Nothing in this module is
    allowed to import an adapter itself: `app.orchestration` may not reach `app.storage`, and
    keeping the entrypoint on the same rule is what stops that contract being routed around.
    """
    raise NotImplementedError(
        "worker composition is not wired yet; see build_worker in app/worker.py"
    )


async def _serve() -> None:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    worker, sweep = build_worker(stop)
    await run_with_sweep(worker, sweep)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info(
        "worker starting: lease %ss, poll %ss, %s job at a time, sweeping every %ss",
        settings.work_lease_seconds,
        settings.work_claim_poll_seconds,
        settings.work_max_concurrent_jobs,
        settings.sweep_interval_seconds,
    )
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
