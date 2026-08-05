"""Failure codes and the one catalog that owns their wording.

D062: `domain/errors.py` holds one `ErrorCode` enum and one `ERROR_CATALOG`, and no message is
written at a raise site. A raise carries a code and, at most, machine-readable details; the
words a learner reads are looked up here. One place to audit for leaks, one place to translate.

Eight codes are `docs/demo.md`, "Errors". `JOB_TIMED_OUT` is the ninth and arrived with the
timeout sweep: it is the second job-failure code, so `docs/demo.md` needs the row.
`plan/14-api-schema.md` names two more (`OUTPUT_PROFILE_NOT_SUPPORTED`,
`DELIVERABLE_INCOMPLETE`) that the demo cannot reach; members are added and never removed
(D072), so those arrive with the stage that raises them.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class ErrorCode(StrEnum):
    """Stable wire values. Text in the database and in JSON, never an integer (D072)."""

    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ARTIFACT_NOT_READY = "ARTIFACT_NOT_READY"
    TOO_MANY_ACTIVE_JOBS = "TOO_MANY_ACTIVE_JOBS"
    GENERATION_FAILED = "GENERATION_FAILED"
    JOB_TIMED_OUT = "JOB_TIMED_OUT"


@dataclass(frozen=True, slots=True)
class ErrorEntry:
    """What a code means at the edge.

    `http_status` is `None` for a code that is a job failure rather than a rejected request:
    a job that fails still answers `200` on `GET /v1/jobs/{id}`, because the request to read a
    failed job did not fail. An edge that is handed such a code has a bug, and the api lane
    maps it to `500` rather than inventing a status here.
    """

    http_status: int | None
    message: str


ERROR_CATALOG: Final[Mapping[ErrorCode, ErrorEntry]] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: ErrorEntry(
            http_status=400,
            message="The request could not be accepted as written.",
        ),
        ErrorCode.UNAUTHENTICATED: ErrorEntry(
            http_status=401,
            message="This request carried no usable identity.",
        ),
        ErrorCode.JOB_NOT_FOUND: ErrorEntry(
            http_status=404,
            message="No such job.",
        ),
        ErrorCode.ARTIFACT_NOT_FOUND: ErrorEntry(
            http_status=404,
            message="No such artifact.",
        ),
        ErrorCode.IDEMPOTENCY_CONFLICT: ErrorEntry(
            http_status=409,
            message="This idempotency key was already used for a different request.",
        ),
        ErrorCode.ARTIFACT_NOT_READY: ErrorEntry(
            http_status=409,
            message="The job has not produced this artifact yet.",
        ),
        ErrorCode.TOO_MANY_ACTIVE_JOBS: ErrorEntry(
            http_status=429,
            message="Too many jobs are already running for this user. Try again shortly.",
        ),
        ErrorCode.GENERATION_FAILED: ErrorEntry(
            http_status=None,
            message="The lesson could not be generated.",
        ),
        ErrorCode.JOB_TIMED_OUT: ErrorEntry(
            http_status=None,
            message="The lesson took too long and was given up on.",
        ),
    }
)
"""Total over `ErrorCode`, enforced by `tests/unit/domain/errors_test.py`.

Two rules the tests hold: a message never carries a placeholder, and a message never repeats
a caller's text. "Not found" that names the id a caller guessed is an enumeration oracle.
"""

_EMPTY_DETAILS: Final[Mapping[str, str]] = MappingProxyType({})


class DomainError(Exception):
    """A code and, optionally, machine-readable details. Never a message.

    `details` is the structured half of the error envelope: which field, which bound. Strings
    only, because every value here is serialised straight to a client and a string cannot
    carry a stray object graph out of the process by accident.
    """

    def __init__(self, code: ErrorCode, details: Mapping[str, str] | None = None) -> None:
        super().__init__(code.value)
        self.code: ErrorCode = code
        self.details: Mapping[str, str] = (
            _EMPTY_DETAILS if details is None else MappingProxyType(dict(details))
        )
