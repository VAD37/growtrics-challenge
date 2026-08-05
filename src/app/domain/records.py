"""The records every lane passes across a seam.

Frozen slotted dataclasses, not pydantic. Pydantic guards trust boundaries -- the HTTP edge, a
step boundary, a worker payload -- and by the time a value is one of these it has already been
through one. Making the domain depend on a validation library would put a framework under the
one package that is supposed to have none, and slots plus `frozen=True` already buy the two
properties that matter here: a typo is an `AttributeError`, and a record cannot be edited by
whoever it was handed to.

Each record is the in-process shape of a row in the frozen schema (`plan/13-mvp.md`, D071),
with the scope override's changes applied: `jobs` carries a `request_key` and no idempotency
key, `requests` is new and immutable, and `chat_context_id` is nullable everywhere (A5, D087).
The one deliberate name difference is `JobRecord.contract_version`, which is the column
`jobs.output_contract` (A4): the column name was frozen before the field split into "what to
make" and "under which rules".
"""

import base64
import binascii
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from app.domain.enums import (
    ArtifactRole,
    Audience,
    ContextKind,
    JobStatus,
    ProfileId,
    ReadingLevel,
    ScanVerdict,
    StageName,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import (
    ArtifactId,
    BriefId,
    ChatContextId,
    JobId,
    PrincipalId,
    RequestKey,
    TraceId,
    WorkItemId,
)

# --------------------------------------------------------------------------- intake


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One labelled piece of context a caller attached to an instruction."""

    kind: ContextKind
    text: str


@dataclass(frozen=True, slots=True)
class JobConstraints:
    """Effective constraints after profile caps, not an echo of the request (D080)."""

    max_duration_s: int
    language: str
    reading_level: ReadingLevel | None


@dataclass(frozen=True, slots=True)
class SubmitJobCommand:
    """What the API hands orchestration: a validated request with no principal in it.

    The caller cannot name whose job this is; that comes from the `AccessScope`. Alongside this
    command the API passes the raw body, so orchestration can write `requests` without the API
    touching a table.
    """

    instruction: str
    context: tuple[ContextItem, ...]
    constraints: JobConstraints
    profile: ProfileId
    chat_context_id: ChatContextId | None


@dataclass(frozen=True, slots=True)
class StoredRequest:
    """The `requests` row: the body exactly as it arrived, keyed by `request_key`.

    Written once and never updated (scope override item 4 and 5). What the user typed and what
    we asked the generator for are two different questions: this record answers the first, and
    `BriefRecord` answers the second. Job status lives on `jobs` and touches nothing here.
    """

    request_key: RequestKey
    principal_id: PrincipalId
    raw: Mapping[str, object]
    # @audit `raw` is untyped on purpose: it is a client-supplied body stored verbatim, and
    # typing it would mean validating it, which would make it no longer verbatim. It is written
    # to a jsonb column and read back only by an operator. Nothing branches on its contents.
    received_at: datetime


@dataclass(frozen=True, slots=True)
class BriefRecord:
    """The `briefs` row: the sealed intermediary product, sole writer `intake` (D066).

    Present here because `BriefRepository` needs a row type; the sealing rules, the guard
    verdict shape, and the template registry belong to the intake lane.
    """

    brief_id: BriefId
    job_id: JobId
    brief_hash: str
    template_version: str
    subject: str
    concept_id: str | None
    instruction: str
    context_items: tuple[ContextItem, ...]
    constraints: JobConstraints
    guard_verdict: Mapping[str, object]
    # @audit `guard_verdict` is untyped: the guard is not built (docs/demo.md cuts it), and its
    # shape is expected to change per rule set. It is jsonb and nothing queries inside it.
    sealed_at: datetime


# --------------------------------------------------------------------------- orchestration


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """The `jobs.failure` jsonb blob, expanded.

    `message` is looked up in `ERROR_CATALOG` by whoever builds this, never written at a raise
    site (D062). The record carries it so a stored failure stays readable after the catalog's
    wording changes.
    """

    code: ErrorCode
    stage: StageName
    message: str
    retryable: bool
    occurred_at: datetime
    trace_id: TraceId


@dataclass(frozen=True, slots=True)
class JobRecord:
    """The `jobs` row: the mutable half of one request.

    `requests` is written once; this is what a status change touches, and nothing else
    (scope override item 5). `version` is the optimistic concurrency counter, `attempt` is
    frozen at 0 for the demo, and `contract_version` is the column `jobs.output_contract` (A4).
    """

    job_id: JobId
    request_key: RequestKey
    principal_id: PrincipalId
    chat_context_id: ChatContextId | None
    status: JobStatus
    stage: StageName
    attempt: int
    progress_percent: int
    profile: ProfileId
    contract_version: str
    constraints: JobConstraints
    failure: FailureRecord | None
    artifact_id: ArtifactId | None
    brief_id: BriefId | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedWorkItem:
    """One `work_items` row, held under a lease.

    Returned by a successful claim and by a successful heartbeat. `claimed_until` is the lease
    edge; nothing in the demo reacts to it passing, see `app/storage/sql/queue.py`.
    """

    item_id: WorkItemId
    job_id: JobId
    claimed_by: str
    claimed_until: datetime
    claim_count: int


# --------------------------------------------------------------------------- custody


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """The `artifacts` row. Sole writer `custody` (D066).

    `principal_id` is denormalised at insert (A6, D088) and `chat_context_id` alongside it
    (D073), so listing a learner's artifacts is one index scan whether or not a chat context
    exists. `role`, `audience`, and `mime` are the three questions the old `kind` folded into
    one (A1, D078), and `scan_verdict` is a fourth: is this file dangerous, not whose it is.
    """

    artifact_id: ArtifactId
    job_id: JobId
    principal_id: PrincipalId
    chat_context_id: ChatContextId | None
    role: ArtifactRole
    audience: Audience
    mime: str
    rel_path: str | None
    size_bytes: int
    content_hash: str
    storage_uri: str
    probe: Mapping[str, object]
    # @audit `probe` is untyped: it is a media probe's output, its keys differ per media type
    # and per prober, and D072 puts exactly that class of value in jsonb. Nothing branches on it
    # and it is never serialised straight to a client.
    scan_verdict: ScanVerdict
    validator_version: str
    published_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContentStream:
    """An open artifact, on its way to a client.

    `chunks` is an async iterator so a 64 MiB video is never held in memory; the domain names
    the type and never awaits it, which is what keeps this package free of I/O. The metadata
    beside it is what the response headers need, decided by custody rather than by the router.
    """

    media_type: str
    size_bytes: int
    content_hash: str
    filename: str
    disposition: Literal["inline", "attachment"]
    chunks: AsyncIterator[bytes]


# --------------------------------------------------------------------------- paging

_CURSOR_SEPARATOR: Final[str] = "\x1f"
"""ASCII unit separator: not legal in any id pattern and not producible by a client."""

_CURSOR_MAX_CHARS: Final[int] = 512
"""A cursor is 60-odd characters. Anything larger is somebody probing the decoder."""


def _invalid_cursor() -> DomainError:
    return DomainError(ErrorCode.INVALID_REQUEST, {"field": "cursor"})


@dataclass(frozen=True, slots=True)
class Cursor:
    """An opaque keyset position over `(created_at, id)`, never an offset (D072).

    Offsets skip and duplicate rows under concurrent inserts, which a job list gets constantly.
    The pair is the sort key of every listing in this system, so a cursor is exactly the last
    row a client saw and the next page is "strictly before this", with the id breaking ties
    between two rows written in the same microsecond.

    Opaque means the encoding is ours to change: a client that decodes one and increments it is
    relying on a shape that is documented as unreadable. Malformed input is
    `INVALID_REQUEST`, never a crash, because a cursor arrives in a query string.
    """

    created_at: datetime
    id: str

    def encode(self) -> str:
        if self.created_at.tzinfo is None:
            raise _invalid_cursor()
        return self._encode_parts(self.created_at.isoformat(), self.id)

    @staticmethod
    def _encode_parts(timestamp: str, id_part: str) -> str:
        """Encode already-rendered halves. Used by `encode` and by tests that forge a token."""
        payload = f"{timestamp}{_CURSOR_SEPARATOR}{id_part}".encode()
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        if not token or len(token) > _CURSOR_MAX_CHARS:
            raise _invalid_cursor()
        padded = token + "=" * (-len(token) % 4)
        try:
            payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise _invalid_cursor() from exc
        timestamp, separator, id_part = payload.partition(_CURSOR_SEPARATOR)
        if not separator or not id_part:
            raise _invalid_cursor()
        try:
            created_at = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise _invalid_cursor() from exc
        if created_at.tzinfo is None:
            raise _invalid_cursor()
        return cls(created_at=created_at, id=id_part)


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a listing. Always an object, never a bare array (D072).

    A bare array cannot grow a `next_cursor` without breaking every parser that consumed it.
    """

    items: tuple[T, ...]
    next_cursor: str | None
