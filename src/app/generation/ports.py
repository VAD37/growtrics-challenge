"""The seam a hostile agent attacks: what we send a worker, and what it claims back.

Two directions, two shapes of type. Going out, `GenerationRequest` is a frozen dataclass we
authored: files, a session id, a trace id, and limits. It carries no `job_id`, no `run_id`, no
`principal_id`, and no `chat_context_id`, because a worker that cannot name anything in this
system cannot address anything in it (`plan/12-data-control.md`).

Coming back, everything is pydantic and everything is a claim. `GenerationBackend.generate`
returns a raw JSON document rather than a parsed object on purpose: a real backend reads that
document off a rented machine, and handing the ACL a pre-parsed value would make the parsing
step something a backend could skip. `generation.acl` is the only module that turns it into
domain types.

The worker never calls us. There is no inbound endpoint from the generation network, so there
is nothing to authenticate, rate limit, or accidentally expose. We poll and we pull.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.brief import BriefBundle
from app.domain.contracts import OutputContract
from app.domain.ids import SessionId, TraceId

MAX_DECLARED_FILES: Final[int] = 256
"""Parser bound, not the contract's bound.

Deliberately looser than `OutputContract.max_file_count` so that a manifest declaring 200 files
is rejected by the ACL with a reason a human can read, rather than by pydantic with a validation
error the trust boundary never got to look at. A cap is still needed here: without one a
manifest declaring a million descriptors is an allocation, not a rejection.
"""

MAX_DECLARED_BYTES: Final[int] = 1 << 40
"""One TiB. Same reasoning: the contract's caps do the real work, this stops the arithmetic."""

_UNTRUSTED: ConfigDict = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
"""Unknown field is a rejection, not a shrug. A worker that sends `storage_uri`, `principal_id`,
or `scan_verdict` finds out immediately that it does not get to write our columns."""


class WorkerStatus(StrEnum):
    """What a worker says happened. Its vocabulary, not ours, which is why it lives here.

    `QUALIFIED` is the interesting member: it is how an agent reports "done, with reservations",
    and it is treated exactly like `COMPLETED`. Our verification decides what arrived either
    way, so a worker cannot lower the bar by admitting to it.
    """

    COMPLETED = "COMPLETED"
    QUALIFIED = "QUALIFIED"
    FAILED = "FAILED"


class RawArtifactDescriptor(BaseModel):
    """One file a worker claims to have written. Every field is a claim."""

    model_config = _UNTRUSTED

    role: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    rel_path: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    size_bytes: Annotated[int, Field(ge=0, le=MAX_DECLARED_BYTES)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


class RawCheckClaim(BaseModel):
    """A check the worker says it ran on its own output.

    Recorded and never used as evidence (`plan/06-trust-boundary.md`). The same checks run again
    on our side over the bytes we received, and the disagreement between the two is itself worth
    watching.
    """

    model_config = _UNTRUSTED

    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    passed: bool


class RawWorkerManifest(BaseModel):
    """The worker's summary of what it made."""

    model_config = _UNTRUSTED

    profile: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    contract_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    checks: Annotated[tuple[RawCheckClaim, ...], Field(max_length=64)] = ()
    duration_s: Annotated[float, Field(ge=0, le=86400)] | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None


class GenerationOutcome(BaseModel):
    """What a worker claims: a session, a status, some descriptors, and a manifest.

    Untrusted in full. Nothing on this model is written to a row and nothing on it is served to
    a client; `generation.acl` turns the parts it accepts into domain types and drops the rest.
    """

    model_config = _UNTRUSTED

    session_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    status: WorkerStatus
    descriptors: Annotated[
        tuple[RawArtifactDescriptor, ...], Field(max_length=MAX_DECLARED_FILES)
    ] = ()
    manifest: RawWorkerManifest


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """The ceilings the backend enforces even if the worker ignores its own.

    @TODO nothing enforces these yet: the mock backend returns a fixture and the sandbox and
    cloud placements are unbuilt (`docs/plan/07-generation.md`, `docs/open-questions.md`). They
    are on the request so the numbers travel with the work rather than living in a backend's
    constructor, and so a backend that grows enforcement does not change this signature.
    """

    wall_clock_s: int
    max_model_calls: int
    max_output_bytes: int


DEFAULT_LIMITS: Final[ExecutionLimits] = ExecutionLimits(
    wall_clock_s=900,
    max_model_calls=64,
    max_output_bytes=83886080,
)
"""80 MiB matches `OutputContract.total_max_bytes` for `video.short.v1`."""


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Everything that crosses to a worker, and nothing else.

    `session_id` is `uuid5` off the job and the attempt, so it is one-way: a worker holding it
    cannot address the job, the run, the principal, or the chat context, and there is nothing in
    the payload to address them with either.
    """

    session_id: SessionId
    trace_id: TraceId
    bundle: BriefBundle
    contract: OutputContract
    limits: ExecutionLimits


@runtime_checkable
class GenerationBackend(Protocol):
    """Where a lesson gets made. One implementation in this build: the mock.

    `generate` returns the raw manifest document, unparsed, because that is what a remote worker
    actually hands back and because parsing it is the ACL's job rather than a backend's.
    """

    async def generate(self, request: GenerationRequest) -> Mapping[str, object]: ...

    async def fetch(self, session_id: str, rel_path: str, *, max_bytes: int) -> bytes: ...
