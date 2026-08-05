"""Failure classification and lease arithmetic. Two pure functions and their edges.

Both are pure on purpose. Classification decides what a learner is told and whether the work is
tried again, and lease arithmetic decides how long a dead worker's row stays unavailable; neither
should need a database to check.
"""

from datetime import timedelta

import pytest
from fakes_test import LEASE_SECONDS, T0, FrozenClock

from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode
from app.orchestration.engine.policy import (
    HEARTBEAT_DIVISOR,
    MIN_HEARTBEAT_SECONDS,
    FailureVerdict,
    classify_failure,
    heartbeat_interval,
    lease_deadline,
    lease_expired,
)

# --------------------------------------------------------------------------- classification


def test_an_unexpected_error_becomes_the_one_job_failure_code() -> None:
    verdict = classify_failure(RuntimeError("ffmpeg is not installed"))
    assert verdict == FailureVerdict(code=ErrorCode.GENERATION_FAILED, retryable=False)


def test_a_job_failure_code_is_kept() -> None:
    verdict = classify_failure(DomainError(ErrorCode.GENERATION_FAILED))
    assert verdict.code is ErrorCode.GENERATION_FAILED


@pytest.mark.parametrize(
    "code",
    [ErrorCode.INVALID_REQUEST, ErrorCode.JOB_NOT_FOUND, ErrorCode.TOO_MANY_ACTIVE_JOBS],
)
def test_a_request_rejection_never_becomes_a_job_failure_verbatim(code: ErrorCode) -> None:
    """A code with an HTTP status describes a rejected request, not a lesson that failed.

    Storing `TOO_MANY_ACTIVE_JOBS` in `jobs.failure` would tell a learner their video failed
    because their request was refused, which is two different events wearing one word.
    """
    assert ERROR_CATALOG[code].http_status is not None
    assert classify_failure(DomainError(code)).code is ErrorCode.GENERATION_FAILED


def test_nothing_is_retryable_in_the_demo() -> None:
    """docs/demo.md D2: the work item is not retried. Backoff is designed, not built."""
    for error in (RuntimeError("x"), DomainError(ErrorCode.GENERATION_FAILED)):
        assert classify_failure(error).retryable is False


# --------------------------------------------------------------------------- leases


def test_the_heartbeat_runs_at_a_third_of_the_lease() -> None:
    assert heartbeat_interval(60) == 60 / HEARTBEAT_DIVISOR
    assert heartbeat_interval(90) == 30.0


def test_a_short_lease_still_gets_a_sane_interval() -> None:
    assert heartbeat_interval(1) == MIN_HEARTBEAT_SECONDS


@pytest.mark.parametrize("lease_seconds", [0, -1])
def test_a_lease_must_have_a_length(lease_seconds: int) -> None:
    with pytest.raises(ValueError, match="lease_seconds"):
        heartbeat_interval(lease_seconds)


def test_two_heartbeats_push_the_deadline_a_full_lease_past_the_last_one() -> None:
    """The arithmetic a dying worker depends on: the deadline is the last beat plus the lease."""
    clock = FrozenClock()
    interval = heartbeat_interval(LEASE_SECONDS)

    assert lease_deadline(clock.now(), LEASE_SECONDS) == T0 + timedelta(seconds=60)
    clock.advance(interval)
    assert lease_deadline(clock.now(), LEASE_SECONDS) == T0 + timedelta(seconds=80)
    clock.advance(interval)
    assert lease_deadline(clock.now(), LEASE_SECONDS) == T0 + timedelta(seconds=100)


def test_a_lease_is_live_until_its_deadline_passes() -> None:
    clock = FrozenClock()
    deadline = lease_deadline(clock.now(), LEASE_SECONDS)

    clock.advance(LEASE_SECONDS - 1)
    assert lease_expired(deadline, clock.now()) is False
    clock.advance(1)
    assert lease_expired(deadline, clock.now()) is True
