"""Two decisions the engine makes that are worth taking out of the engine: what a failure means,
and how long a claim is good for.

Both are pure functions over values. Classification decides what a learner is told and whether
the work is tried again; lease arithmetic decides how long a dead worker's row stays unavailable.
Neither should need a database to check, and neither should be inlined into a `try` block where
it can only be tested by causing the failure it classifies.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode

# --------------------------------------------------------------------------- failures


@dataclass(frozen=True, slots=True)
class FailureVerdict:
    """What a caught exception means for the job that was running when it was raised."""

    code: ErrorCode
    retryable: bool


def classify_failure(error: BaseException) -> FailureVerdict:
    """Map anything a step can raise onto one job failure code.

    A code with an HTTP status in `ERROR_CATALOG` describes a **rejected request**, not a lesson
    that failed. `TOO_MANY_ACTIVE_JOBS` in `jobs.failure` would tell a learner their video failed
    because their request was refused, which is two different events wearing one word. So only a
    code the catalog marks with no status survives; everything else becomes `GENERATION_FAILED`,
    which is the one job failure code the demo has (docs/demo.md, "Errors").

    Nothing is retryable. docs/demo.md D2: the work item is not retried, the failure is final,
    and the queue row is removed rather than made available again.

    @TODO retry classification with backoff and jitter (docs/plan/05-workflow-engine.md). It
    needs a retryable class of failure, an attempt counter that moves (`jobs.attempt` is frozen
    at 0 today) and the `work_items.available_at` column, which already exists.
    """
    if isinstance(error, DomainError) and ERROR_CATALOG[error.code].http_status is None:
        return FailureVerdict(code=error.code, retryable=False)
    return FailureVerdict(code=ErrorCode.GENERATION_FAILED, retryable=False)


# --------------------------------------------------------------------------- leases

HEARTBEAT_DIVISOR: Final[int] = 3
"""Renew at a third of the lease, so two beats can be lost before the claim is at risk."""

MIN_HEARTBEAT_SECONDS: Final[float] = 1.0
"""A floor, so a misconfigured one-second lease does not turn into a busy loop on the database."""


def heartbeat_interval(lease_seconds: int) -> float:
    """How often a holder should renew a lease of this length."""
    if lease_seconds < 1:
        raise ValueError(f"lease_seconds must be at least 1, got {lease_seconds}")
    return max(lease_seconds / HEARTBEAT_DIVISOR, MIN_HEARTBEAT_SECONDS)


def lease_deadline(now: datetime, lease_seconds: int) -> datetime:
    """The `work_items.claimed_until` a claim or a heartbeat writes."""
    return now + timedelta(seconds=lease_seconds)


def lease_expired(claimed_until: datetime, now: datetime) -> bool:
    """Whether a claim has lapsed. Inclusive at the deadline: at the edge, the holder is late.

    The predicate `WorkQueue.reclaim` and `WorkQueue.exhausted` split lapsed rows on. Imported by
    the adapters rather than restated, because "inclusive at the deadline" is a decision and a
    second copy of `<=` is a decision nobody knows they are changing.
    """
    return claimed_until <= now
