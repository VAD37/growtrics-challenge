"""Two timeouts and the ticker that runs them.

They are twins, and conflating them hides both:

- **Pending too long.** A job sits `QUEUED` because no worker ever claimed it. Past
  `config.job_pending_max_seconds` it is `FAILED` and its `work_items` row goes with it. This is
  the release valve on a backlog: at fifty thousand items deep the honest answer to a job nobody
  will reach today is a failure a learner can see, not a queue that grows without bound.
- **Lease expired.** A worker died mid-job, so `work_items.claimed_until` is in the past while the
  job sits `RUNNING`. The row is handed back to be claimed again, and a row that has burned
  `config.work_max_claims` workers is failed instead, because a poison item handed back forever
  is a queue that never drains either.

Everything here goes through `WorkQueue` and `JobRepository`. No SQL, and the failure it writes is
the same shape a step failure writes: one `apply_transition` (D066), a `FailureRecord` whose words
come from `ERROR_CATALOG` (D062), and no retry (docs/demo.md D2). `JOB_TIMED_OUT` is the code, and
it carries no HTTP status because a job that timed out still answers `200` on
`GET /v1/jobs/{id}` -- see `policy.classify_failure`.

It uses `work_items` and `jobs` only. `workflow_runs` is not created in migration 1 (D093), and
its `runs_expired_leases` index is the shape this would have used if it existed. The three columns
on `work_items` are enough, which is why this cost no schema change to build.

Where it runs is half the feature: `run_sweeper` is a task in the worker process beside the claim
loop, never a request handler. A handler dies with its request and a backlog does not, and a
backlog that is only swept while somebody is polling is not swept.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.config import settings
from app.domain.enums import TERMINAL_JOB_STATUSES, JobStatus, StageName
from app.domain.errors import ERROR_CATALOG, ErrorCode
from app.domain.ids import derive_trace_id, derive_work_item_id
from app.domain.records import FailureRecord, JobRecord
from app.orchestration.ports import Clock, JobRepository, JobTransition, WorkQueue

logger = logging.getLogger(__name__)

type SleepFn = Callable[[float], Awaitable[None]]
"""Injected so a test can drive the ticker at full speed. Production passes `asyncio.sleep`."""

SWEEP_BATCH_LIMIT: Final[int] = 500
"""Rows one tick reads per query.

The backlog this exists for is tens of thousands deep. A sweep that read all of it would trade
one unbounded queue for one unbounded result set, and the next tick is a minute away.
"""


@dataclass(frozen=True, slots=True)
class SweepThresholds:
    """Every number a tick runs on, resolved from settings in one place.

    A bundle rather than four keyword arguments, so a test changes one threshold without
    restating the other three, and so no sweep body can reach for a literal.
    """

    pending_max_seconds: int = settings.job_pending_max_seconds
    stale_after_seconds: int = settings.job_stale_seconds
    max_claims: int = settings.work_max_claims
    batch_limit: int = SWEEP_BATCH_LIMIT


DEFAULT_THRESHOLDS: Final[SweepThresholds] = SweepThresholds()
"""The settings-derived defaults, built once. Frozen, so sharing one instance is safe."""


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one tick did.

    All zeros is the steady state, and it is also what the second of two identical ticks returns:
    a sweep that keeps finding work it already did is a sweep that is failing the same job twice.
    """

    pending_failed: int
    stale_failed: int
    leases_reclaimed: int

    @property
    def touched(self) -> int:
        return self.pending_failed + self.stale_failed + self.leases_reclaimed


async def _give_up_on(
    jobs: JobRepository,
    queue: WorkQueue,
    job: JobRecord,
    *,
    now: datetime,
) -> bool:
    """Fail one job and take its queue row with it. `False` when the job had already moved on.

    The terminal check is invariant 4 of `VideoJob`: a `SUCCEEDED` job that a slow read handed to
    the sweep is not a job to fail. The version on the transition is the other half of that guard,
    because a runner that moved the row between the read and the write bumped it.

    The queue row goes second and unconditionally. A `FAILED` job whose `work_items` row survived
    is a row the next worker claims and the runner then drops, every time, forever.
    """
    if job.status in TERMINAL_JOB_STATUSES:
        return False
    failure = FailureRecord(
        code=ErrorCode.JOB_TIMED_OUT,
        stage=job.stage,
        message=ERROR_CATALOG[ErrorCode.JOB_TIMED_OUT].message,
        retryable=False,
        occurred_at=now,
        trace_id=derive_trace_id(job.job_id, job.attempt),
    )
    await jobs.apply_transition(
        JobTransition(
            job_id=job.job_id,
            expected_version=job.version,
            status=JobStatus.FAILED,
            stage=StageName.FAILED,
            # `StageName.FAILED` has no percent and the bar never goes backwards (D042).
            progress_percent=job.progress_percent,
            failure=failure,
        )
    )
    await queue.discard(derive_work_item_id(job.job_id))
    return True


async def fail_pending_jobs(
    jobs: JobRepository,
    queue: WorkQueue,
    clock: Clock,
    *,
    thresholds: SweepThresholds = DEFAULT_THRESHOLDS,
) -> int:
    """Fail every job that has waited past `config.job_pending_max_seconds` to be claimed.

    Returns the jobs given up on. A `QUEUED` job's row has not been written since it was
    submitted, so "untouched since" and "submitted before" are the same cutoff here.

    There is a window where a worker has claimed the row but the runner has not written `RUNNING`
    yet, so the job still reads as pending. It is one transaction wide against a threshold of an
    hour, and `expected_version` turns the race into a rejected write rather than into a job
    killed on the way to starting.
    """
    now = clock.now()
    cutoff = now - timedelta(seconds=thresholds.pending_max_seconds)
    waiting = await jobs.list_untouched_since(
        JobStatus.QUEUED, cutoff, limit=thresholds.batch_limit
    )
    failed = 0
    for job in waiting:
        if await _give_up_on(jobs, queue, job, now=now):
            failed += 1
    if failed:
        logger.warning(
            "failed %s job(s) that waited longer than %ss to be claimed",
            failed,
            thresholds.pending_max_seconds,
        )
    return failed


async def fail_stale_jobs(
    jobs: JobRepository,
    queue: WorkQueue,
    clock: Clock,
    *,
    thresholds: SweepThresholds = DEFAULT_THRESHOLDS,
) -> int:
    """Two triggers, one outcome. Returns the jobs given up on.

    An item whose lease has lapsed for the `max_claims`-th time has burned every worker it is
    going to get. A job that has been `RUNNING` and untouched for longer than
    `config.job_stale_seconds` has stopped moving with nobody left to notice: `jobs.updated_at`
    advances on every stage transition, so fifteen minutes inside one stage is a hang rather than
    a slow render.

    Same terminal shape as a step failure, because it is the same event: the lesson did not get
    made.
    """
    now = clock.now()
    failed = 0

    for item in await queue.exhausted(now, thresholds.max_claims):
        job = await jobs.load_for_run(item.job_id)
        if job is not None and await _give_up_on(jobs, queue, job, now=now):
            failed += 1
            continue
        # The job is gone, or it finished while the row was being worn out. Either way nobody
        # can fail it and nobody should run it, so the row is all there is left to remove.
        await queue.discard(item.item_id)

    cutoff = now - timedelta(seconds=thresholds.stale_after_seconds)
    for job in await jobs.list_untouched_since(
        JobStatus.RUNNING, cutoff, limit=thresholds.batch_limit
    ):
        if await _give_up_on(jobs, queue, job, now=now):
            failed += 1

    if failed:
        logger.warning("failed %s job(s) that stopped moving", failed)
    return failed


async def reclaim_expired_leases(
    queue: WorkQueue,
    clock: Clock,
    *,
    thresholds: SweepThresholds = DEFAULT_THRESHOLDS,
) -> int:
    """Hand every lapsed claim back to the queue. Returns the rows made claimable again.

    Rows already at `max_claims` stay where they are; `fail_stale_jobs` has just dealt with them.
    """
    reclaimed = await queue.reclaim(clock.now(), thresholds.max_claims)
    if reclaimed:
        logger.info("handed %s work item(s) back after their lease lapsed", reclaimed)
    return reclaimed


async def sweep_once(
    queue: WorkQueue,
    jobs: JobRepository,
    clock: Clock,
    *,
    thresholds: SweepThresholds = DEFAULT_THRESHOLDS,
) -> SweepReport:
    """One pass, in the order that leaves nothing for the next pass to clean up.

    Give up first, hand back second. Reclaiming ahead of the failures would make a row claimable
    that the same tick is about to fail, and a worker would pick up a job on its way to `FAILED`.
    """
    pending = await fail_pending_jobs(jobs, queue, clock, thresholds=thresholds)
    stale = await fail_stale_jobs(jobs, queue, clock, thresholds=thresholds)
    reclaimed = await reclaim_expired_leases(queue, clock, thresholds=thresholds)
    return SweepReport(pending_failed=pending, stale_failed=stale, leases_reclaimed=reclaimed)


async def run_sweeper(
    queue: WorkQueue,
    jobs: JobRepository,
    clock: Clock,
    *,
    stop: asyncio.Event,
    interval_seconds: float = settings.sweep_interval_seconds,
    thresholds: SweepThresholds = DEFAULT_THRESHOLDS,
    sleep: SleepFn = asyncio.sleep,
) -> None:
    """Sweep every `config.sweep_interval_seconds` until told to stop.

    A tick that raises is logged and the next one still happens. The sweep shares a process with
    the claim loop, and a database blip in a background scan must not be able to stop the worker
    claiming: the thing that fails jobs is less important than the thing that finishes them.
    """
    logger.info("sweeping every %ss", interval_seconds)
    while not stop.is_set():
        try:
            report = await sweep_once(queue, jobs, clock, thresholds=thresholds)
        except Exception:
            logger.exception("a sweep tick failed; the ticker carries on")
        else:
            if report.touched:
                logger.info(
                    "sweep: %s pending failed, %s stale failed, %s leases reclaimed",
                    report.pending_failed,
                    report.stale_failed,
                    report.leases_reclaimed,
                )
        await _wait_to_tick(stop, interval_seconds, sleep)
    logger.info("sweeper stopped")


async def _wait_to_tick(stop: asyncio.Event, interval_seconds: float, sleep: SleepFn) -> None:
    """Idle for the interval, but wake at once on a stop signal.

    A race rather than a timeout. A sweeper that only noticed SIGTERM at the end of its nap would
    add the whole interval to every deployment.
    """
    stopping = asyncio.ensure_future(stop.wait())
    napping = asyncio.ensure_future(sleep(interval_seconds))
    try:
        await asyncio.wait({stopping, napping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stopping, napping):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
