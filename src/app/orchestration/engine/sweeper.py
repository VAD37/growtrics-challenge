"""Lease expiry, queue reclaim and stale-job detection. DEFERRED: signatures only.

Scope override item 8. The worker still CLAIMS with a lease and heartbeats it; what is deferred
is everything that REACTS to a lease running out. Nothing here has a body and nothing starts a
sweeper task.

**The consequence, stated rather than hidden: until this exists, a worker that dies mid-job
leaves its `work_items` row claimed forever and its job sits at `RUNNING` until an operator opens
psql.** There is no timeout, no reclaim, no dead-letter path and no alarm. A demo run that is
interrupted has to be cleaned up by hand.

Picking this up is filling in three bodies, and everything they need already exists:

- `work_items.claimed_by`, `work_items.claimed_until` and `work_items.claim_count` are in
  migration 1, and `work_items_claimable` indexes the available rows.
- `config.work_max_claims` and `config.job_stale_seconds` hold the two numbers.
- `WorkQueue.reclaim` is declared on the port in `app/orchestration/ports.py`.
- `policy.lease_expired` is the predicate.

It uses `work_items` only. `workflow_runs` is not created in migration 1 (D093), and its
`runs_expired_leases` index is the shape this would have used if it were. The three columns on
`work_items` are enough, which is why the deferral costs no schema change to undo.
"""

from app.config import settings
from app.orchestration.ports import Clock, JobRepository, WorkQueue


async def reclaim_expired_leases(
    queue: WorkQueue,
    clock: Clock,
    *,
    max_claims: int = settings.work_max_claims,
) -> int:
    """@TODO DEFERRED (scope override item 8). Return the rows made claimable again.

    Clears `claimed_by` and `claimed_until` on every `work_items` row where
    `claimed_until < clock.now()` and the job is unfinished, incrementing `claim_count` as it
    goes, so the item is claimable by the next worker to poll. A row already past `max_claims`
    is not handed back; `fail_stale_jobs` deals with it.
    """
    raise NotImplementedError("queue reclaim is deferred; see the module docstring")


async def fail_stale_jobs(
    jobs: JobRepository,
    clock: Clock,
    *,
    stale_after_seconds: int = settings.job_stale_seconds,
) -> int:
    """@TODO DEFERRED (scope override item 8). Return the jobs given up on.

    Two triggers, one outcome. A work item whose `claim_count` has passed
    `config.work_max_claims`, or a job that has been `RUNNING` and untouched for longer than
    `stale_after_seconds`, becomes `FAILED` with `GENERATION_FAILED` through
    `JobRepository.apply_transition`, and its work item is removed. Same terminal shape as a step
    failure, because it is the same event: the lesson did not get made.
    """
    raise NotImplementedError("stale job detection is deferred; see the module docstring")


async def run_sweeper(
    queue: WorkQueue,
    jobs: JobRepository,
    clock: Clock,
    *,
    interval_seconds: float = 30.0,
) -> None:
    """@TODO DEFERRED (scope override item 8). The timer that would call the two above.

    Deliberately not started by `app/worker.py`. A sweeper that runs and does nothing is worse
    than an absent one: it looks like the timeout system exists.
    """
    raise NotImplementedError("the sweeper is deferred; see the module docstring")
