"""Identifier types and the derivation spine.

Every id this service mints is `uuid5` off the **request key**, rendered as Crockford base32 of
the sixteen bytes behind a type prefix (`plan/14-api-schema.md`, D072).

    request_key  = "req_" + crockford(uuid4 bytes)      <- minted, once, per POST /v1/jobs
    job_id       = uuid5(NS_JOB,       request_key)
    brief_id     = uuid5(NS_BRIEF,     job_id)
    work_item_id = uuid5(NS_WORK_ITEM, job_id)
    artifact_id  = uuid5(NS_ART,       job_id | content_hash)

`request_key` is the only non-deterministic input in the system, and `new_request_key` is the
only function here that reads a random source. Everything downstream is a pure function of it,
so an id can still be computed before its row exists and both sides of a seam agree on it.
That is what makes at-least-once delivery safe by construction.

Supersedes D055 and D089, see the scope override: there is no `Idempotency-Key`, no
`idempotency_keys` table, and no replay protection. Two submits of the same body mint two
request keys and therefore become two jobs. The spine is otherwise unchanged, and restoring
replay protection means deriving the request key from a client key rather than from entropy.

`join_id_parts` escapes the separator before joining. The docs spell the spine with a bare `|`,
and for well-formed inputs this produces exactly that string; the escaping only changes the
result for a part that itself contains `|` or `\\`, where an unescaped join would let one field
impersonate a boundary and collide two different names onto one id.
"""

import re
from typing import Annotated, Final
from uuid import UUID, uuid4, uuid5

from pydantic import StringConstraints

from app.domain.errors import DomainError, ErrorCode

# --------------------------------------------------------------------------- encoding

CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
"""Crockford base32: the digits and the uppercase letters minus I, L, O, and U."""

_ALPHABET_INDEX: Final[dict[str, int]] = {
    char: position for position, char in enumerate(CROCKFORD_ALPHABET)
}

ID_BYTE_LENGTH: Final[int] = 16
ID_BODY_LENGTH: Final[int] = 26
"""26 base32 characters carry 130 bits, so the top two bits of the body are always zero."""

ID_SEPARATOR: Final[str] = "|"
_ESCAPE: Final[str] = "\\"


def encode_crockford(raw: bytes) -> str:
    """Render sixteen bytes as 26 Crockford base32 characters, big-endian, unpadded."""
    if len(raw) != ID_BYTE_LENGTH:
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "raw", "expected": "16 bytes"})
    value = int.from_bytes(raw, "big")
    out: list[str] = ["0"] * ID_BODY_LENGTH
    for position in range(ID_BODY_LENGTH - 1, -1, -1):
        out[position] = CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def decode_crockford(text: str) -> bytes:
    """Reverse `encode_crockford`.

    Strict on purpose: uppercase only, exactly 26 characters, and no value wider than sixteen
    bytes. Crockford's own spec folds case and treats `I`/`L` as `1`; we never hand-type these
    ids, and one canonical form means id equality stays string equality.
    """
    if len(text) != ID_BODY_LENGTH:
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "id", "expected": "26 characters"})
    value = 0
    for char in text:
        digit = _ALPHABET_INDEX.get(char)
        if digit is None:
            raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "id", "expected": "base32"})
        value = (value << 5) | digit
    if value >= 1 << (ID_BYTE_LENGTH * 8):
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "id", "expected": "128 bits"})
    return value.to_bytes(ID_BYTE_LENGTH, "big")


def join_id_parts(*parts: str) -> str:
    """Join derivation inputs into one unambiguous name. See the module docstring."""
    escaped = [
        part.replace(_ESCAPE, _ESCAPE + _ESCAPE).replace(ID_SEPARATOR, _ESCAPE + ID_SEPARATOR)
        for part in parts
    ]
    return ID_SEPARATOR.join(escaped)


# --------------------------------------------------------------------------- patterns

_BODY: Final[str] = "[0-9A-HJKMNP-TV-Z]{26}"

JOB_ID_PATTERN: Final[str] = rf"^job_{_BODY}$"
ARTIFACT_ID_PATTERN: Final[str] = rf"^art_{_BODY}$"
BRIEF_ID_PATTERN: Final[str] = rf"^brf_{_BODY}$"
TRACE_ID_PATTERN: Final[str] = rf"^tr_{_BODY}$"
WORK_ITEM_ID_PATTERN: Final[str] = rf"^wi_{_BODY}$"
SESSION_ID_PATTERN: Final[str] = rf"^ses_{_BODY}$"
REQUEST_KEY_PATTERN: Final[str] = rf"^req_{_BODY}$"
PRINCIPAL_ID_PATTERN: Final[str] = r"^u_[A-Za-z0-9_.:-]{1,64}$"
CHAT_CONTEXT_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_.:-]{1,128}$"

type JobId = Annotated[str, StringConstraints(pattern=JOB_ID_PATTERN)]
type ArtifactId = Annotated[str, StringConstraints(pattern=ARTIFACT_ID_PATTERN)]
type BriefId = Annotated[str, StringConstraints(pattern=BRIEF_ID_PATTERN)]
type TraceId = Annotated[str, StringConstraints(pattern=TRACE_ID_PATTERN)]
type WorkItemId = Annotated[str, StringConstraints(pattern=WORK_ITEM_ID_PATTERN)]
type SessionId = Annotated[str, StringConstraints(pattern=SESSION_ID_PATTERN)]
type PrincipalId = Annotated[str, StringConstraints(pattern=PRINCIPAL_ID_PATTERN)]

type RequestKey = Annotated[str, StringConstraints(pattern=REQUEST_KEY_PATTERN)]
"""The linking key one request is known by, everywhere in the database.

Server-minted, never accepted from a client, and the spine every other id hangs off. It is what
joins the `requests` row, the `jobs` row, the brief, the queue item, and the artifacts of one
submission into one story.
"""

type ChatContextId = Annotated[str, StringConstraints(pattern=CHAT_CONTEXT_ID_PATTERN)]
"""Foreign. We index it and do not mint it, so it is an opaque token and nothing more.

@audit the pattern stops path and header injection; it does not make the id ours.
Claim-on-first-use (D068) is safe only while the minting side keeps these unguessable (Q-W).
"""

_JOB_ID_RE: Final[re.Pattern[str]] = re.compile(JOB_ID_PATTERN)
_ARTIFACT_ID_RE: Final[re.Pattern[str]] = re.compile(ARTIFACT_ID_PATTERN)
_BRIEF_ID_RE: Final[re.Pattern[str]] = re.compile(BRIEF_ID_PATTERN)
_TRACE_ID_RE: Final[re.Pattern[str]] = re.compile(TRACE_ID_PATTERN)
_WORK_ITEM_ID_RE: Final[re.Pattern[str]] = re.compile(WORK_ITEM_ID_PATTERN)
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(SESSION_ID_PATTERN)
_REQUEST_KEY_RE: Final[re.Pattern[str]] = re.compile(REQUEST_KEY_PATTERN)
_PRINCIPAL_ID_RE: Final[re.Pattern[str]] = re.compile(PRINCIPAL_ID_PATTERN)
_CHAT_CONTEXT_ID_RE: Final[re.Pattern[str]] = re.compile(CHAT_CONTEXT_ID_PATTERN)


def is_job_id(value: str) -> bool:
    return _JOB_ID_RE.fullmatch(value) is not None


def is_artifact_id(value: str) -> bool:
    return _ARTIFACT_ID_RE.fullmatch(value) is not None


def is_brief_id(value: str) -> bool:
    return _BRIEF_ID_RE.fullmatch(value) is not None


def is_trace_id(value: str) -> bool:
    return _TRACE_ID_RE.fullmatch(value) is not None


def is_work_item_id(value: str) -> bool:
    return _WORK_ITEM_ID_RE.fullmatch(value) is not None


def is_session_id(value: str) -> bool:
    return _SESSION_ID_RE.fullmatch(value) is not None


def is_request_key(value: str) -> bool:
    return _REQUEST_KEY_RE.fullmatch(value) is not None


def is_principal_id(value: str) -> bool:
    return _PRINCIPAL_ID_RE.fullmatch(value) is not None


def is_chat_context_id(value: str) -> bool:
    return _CHAT_CONTEXT_ID_RE.fullmatch(value) is not None


# --------------------------------------------------------------------------- namespaces

# Pinned literals. The recipe that produced them, checked by `tests/unit/domain/ids_test.py`:
#     root = uuid5(NAMESPACE_DNS, "ids.growtrics-challenge.invalid")
#     NS_JOB = uuid5(root, "job"), NS_BRIEF = uuid5(root, "brief"), NS_ART = uuid5(root,
#     "artifact"), NS_WORK_ITEM = uuid5(root, "work_item"), and so on.
# The literal is what ships. Changing one stops every id in every database being derivable.
NS_JOB: Final[UUID] = UUID("f50c4698-3f66-55a0-af08-9be8efd7c715")
NS_BRIEF: Final[UUID] = UUID("8d3dddd1-c277-5610-9838-d8358d40a811")
NS_ART: Final[UUID] = UUID("29669cfd-e4ba-540d-bd22-a872f24fdbe3")
NS_WORK_ITEM: Final[UUID] = UUID("40832641-47e3-56db-b1db-d9f13e81ba22")
NS_TRACE: Final[UUID] = UUID("2e715f24-09ea-5cc5-bd9f-65ec7b994fd8")
NS_SESSION: Final[UUID] = UUID("f9b79c27-84d7-588d-aa70-1b740b195490")


def _derive(namespace: UUID, prefix: str, name: str) -> str:
    return f"{prefix}{encode_crockford(uuid5(namespace, name).bytes)}"


# --------------------------------------------------------------------------- minting


def mint_request_key(entropy: bytes) -> RequestKey:
    """Mint the linking key for one submitted request (supersedes D055/D089, scope override).

    Takes its sixteen bytes as an argument rather than reading a random source itself, so this
    stays a pure function and the caller owns the entropy. `new_request_key` is the thin
    wrapper that supplies `uuid4` bytes; use that at the edge and this one in a test.
    """
    return f"req_{encode_crockford(entropy)}"


def new_request_key() -> RequestKey:
    """The one non-deterministic call in this module, and in the id spine as a whole.

    Every other id is a pure function of the key this returns, which is why the boundary is
    drawn here rather than scattered across the callers that need an id.
    """
    return mint_request_key(uuid4().bytes)


# --------------------------------------------------------------------------- derivation


def derive_job_id(request_key: RequestKey) -> JobId:
    """The root of the spine: `uuid5(NS_JOB, request_key)` (scope override, supersedes D055).

    The principal and the chat context are no longer inputs. A job belongs to whoever submitted
    it, recorded on the row; folding them into the id said the same thing twice and made two
    identical submissions by one user collide onto one job, which is exactly the replay
    behaviour the override removed.
    """
    return _derive(NS_JOB, "job_", request_key)


def derive_brief_id(job_id: JobId) -> BriefId:
    """One sealed brief per job: invariant 3 of `VideoJob` makes a new brief a new job."""
    return _derive(NS_BRIEF, "brf_", job_id)


def derive_artifact_id(job_id: JobId, content_hash: str) -> ArtifactId:
    """`uuid5(NS_ART, job_id | content_hash)` (D055, kept by the scope override).

    Job-scoped by construction, so a repeat query renders again rather than serving another
    learner's bytes. Cross-job reuse is parked as Q-AC.
    """
    return _derive(NS_ART, "art_", join_id_parts(job_id, content_hash))


def derive_work_item_id(job_id: JobId) -> WorkItemId:
    """One queue row per job, so an at-least-once enqueue collides on the primary key."""
    return _derive(NS_WORK_ITEM, "wi_", job_id)


def derive_trace_id(job_id: JobId, attempt: int) -> TraceId:
    """Stable per attempt, so a retry's log lines do not merge into the previous try's trace."""
    return _derive(NS_TRACE, "tr_", join_id_parts(job_id, str(attempt)))


def derive_session_id(job_id: JobId, attempt: int) -> SessionId:
    """The only identity that crosses to the worker (`plan/12-data-control.md`).

    uuid5 is one-way, so a worker holding this cannot address the job, the run, the principal,
    or the chat context. There is nothing in the payload to address them with either.
    """
    return _derive(NS_SESSION, "ses_", join_id_parts(job_id, str(attempt)))
