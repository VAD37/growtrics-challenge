"""Every seam orchestration reaches through, as `typing.Protocol`.

Structural typing, so an adapter satisfies a port by having the right methods and never by
importing this module. That is what keeps the import graph pointing one way: `app.orchestration`
imports `app.domain` and nothing else of ours, and the import-linter contract in `pyproject.toml`
holds it there.

Three shapes carry more weight than the rest, so they are named here rather than left to a
convention:

`Submission` is the three rows one `POST /v1/jobs` writes. It is one value because `UnitOfWork`
takes one value: there is no port method anywhere that can write the `jobs` row without the
`requests` row beside it, so a half-written submit is not a mistake a caller can make.

`JobTransition` is the only shape that changes a `jobs` row, and exactly one method takes it.
"Only orchestration writes job status" (`plan/12-data-control.md`, D066) is then a property of
this file rather than a rule somebody has to remember.

`ArtifactDescriptor` is what an agent claims it produced. It is pydantic rather than a dataclass
because it is a trust boundary, and every field on it is the worker's word.

Supersedes D055 and D089 where they assume an idempotency key, and D060/D068 where they assume an
ownership predicate; see the scope override. The `AccessScope` argument survives on every
caller-facing read (D067) precisely so that restoring the check fills in bodies instead of
re-signing methods.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.access import AccessScope
from app.domain.enums import ArtifactRole, JobStatus, ProfileId, StageName
from app.domain.ids import (
    ArtifactId,
    BriefId,
    ChatContextId,
    JobId,
    PrincipalId,
    RequestKey,
    SessionId,
    TraceId,
    WorkItemId,
)
from app.domain.records import (
    ArtifactRecord,
    BriefRecord,
    ClaimedWorkItem,
    ContentStream,
    Cursor,
    FailureRecord,
    JobConstraints,
    JobRecord,
    Page,
    StoredRequest,
)

MAX_DESCRIPTORS: Final[int] = 24
"""`video.short.v1` allows 24 files in one deliverable (`plan/14-api-schema.md`).

The cap lives on the inbound model so an oversized manifest is rejected before custody opens a
single file, rather than after it has opened twenty-five.
"""

# --------------------------------------------------------------------------- what a worker claims


class ArtifactDescriptor(BaseModel):
    """One file the agent CLAIMS to have produced. A claim, not evidence.

    Pydantic and not a dataclass because this is where untrusted data re-enters the system
    (`plan/12-data-control.md`, "generation to custody"). `extra="forbid"` so a worker cannot
    smuggle a field custody has never heard of, `frozen=True` so what was validated is what is
    used, and `str_strip_whitespace=True` so a trailing newline in a manifest is not a different
    media type.

    @audit nothing on this model is believed. `media_type` and `size_bytes` are re-derived by
    custody from the bytes it harvests, and `source_uri` is matched against the harvest allowlist
    before anything opens it. The validation here only bounds the shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    role: ArtifactRole
    media_type: Annotated[str, StringConstraints(min_length=3, max_length=128)]
    size_bytes: Annotated[int, Field(ge=0)]
    source_uri: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class GenerationOutcome(BaseModel):
    """What one generation attempt handed back, before anything has been verified.

    `session_id` is the only identity that crossed to the worker, and it is validated on the way
    back so a worker cannot answer for a session it was never given.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: SessionId
    descriptors: Annotated[tuple[ArtifactDescriptor, ...], Field(max_length=MAX_DESCRIPTORS)]


# --------------------------------------------------------------------------- what crosses inward


@dataclass(frozen=True, slots=True)
class QueuedWorkItem:
    """A `work_items` row as first inserted: available now, claimed by nobody.

    `item_id` is `derive_work_item_id(job_id)`, so an at-least-once enqueue collides on the
    primary key instead of queueing the same job twice.
    """

    item_id: WorkItemId
    job_id: JobId
    available_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Submission:
    """The three rows one submit writes: what they typed, what we are doing about it, and the
    queue entry that makes it happen.

    Every field is mandatory, and `UnitOfWork` takes the whole value. Writing the job row without
    the request row is not a thing this port can express.
    """

    request: StoredRequest
    job: JobRecord
    work_item: QueuedWorkItem


@dataclass(frozen=True, slots=True)
class JobTransition:
    """The only shape that moves a `jobs` row (D066).

    `expected_version` is optimistic concurrency: an adapter matches on
    `(job_id, version = expected_version)` and treats zero updated rows as a conflict. The demo
    runs one worker, so a conflict means a bug rather than contention.

    `brief_id`, `artifact_id` and `failure` are `None` for "leave the column as it is", never for
    "clear it". Nothing in the demo clears any of the three: a failed job is not retried, so its
    failure is final (docs/demo.md D2).

    There is no `message` field. A failure's words come from `ERROR_CATALOG` (D062), and the
    caller builds the `FailureRecord` from a code.
    """

    job_id: JobId
    expected_version: int
    status: JobStatus
    stage: StageName
    progress_percent: int
    brief_id: BriefId | None = None
    artifact_id: ArtifactId | None = None
    failure: FailureRecord | None = None


@dataclass(frozen=True, slots=True)
class HarvestOutcome:
    """What custody returns once it has harvested, verified, stored and inserted.

    `primary` is the row named by `jobs.artifact_id`, and it is in `artifacts` as well. One list
    and one lookup, so nothing has to answer "is it in both places" at serialisation time.
    """

    primary: ArtifactRecord
    artifacts: tuple[ArtifactRecord, ...]


# --------------------------------------------------------------------------- ports


@runtime_checkable
class Clock(Protocol):
    """Time, injected. A frozen clock in a test is then the same object as the real one."""

    def now(self) -> datetime: ...


@runtime_checkable
class AdmissionPolicy(Protocol):
    """The one gate in front of `POST /v1/jobs` (D090)."""

    async def admit(self, scope: AccessScope) -> None:
        """Raise `DomainError(TOO_MANY_ACTIVE_JOBS)` when this caller may not start another job."""
        ...


@runtime_checkable
class JobRepository(Protocol):
    """The `jobs` table. Reads take a scope; one method writes."""

    async def count_active(self, scope: AccessScope, principal_id: PrincipalId) -> int:
        """`QUEUED` or `RUNNING` jobs for one principal, through `jobs_active` (D090).

        `principal_id` is the key of the count, not an authorisation predicate: it is applied
        whatever the scope says, because a capacity gate that ignored it would let one busy
        caller lock out everybody else.
        """
        ...

    async def get(self, scope: AccessScope, job_id: JobId) -> JobRecord | None:
        """@audit no ownership check: the scope is carried and not consulted (override item 1)."""
        ...

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        """Newest first, keyset paged over `(created_at, job_id)`.

        @audit no ownership check. This returns every job in the database, whoever owns it.
        """
        ...

    async def load_for_run(self, job_id: JobId) -> JobRecord | None:
        """The row a claimed work item points at.

        No scope, because there is no caller: this is the system reading its own row on the way
        to executing it. It is reachable from the worker and from nowhere the API can touch.
        """
        ...

    async def list_untouched_since(
        self, status: JobStatus, moment: datetime, *, limit: int
    ) -> tuple[JobRecord, ...]:
        """Jobs still in `status` whose row has not been written since `moment`, oldest first.

        Inclusive at `moment`, which is `policy.lease_expired`'s convention: at the edge, the
        wait is over.

        The sweep's read (`engine/sweeper.py`). No scope for the same reason `load_for_run` has
        none: the system is reading its own backlog, and a scope here would imply a caller who
        could ask for somebody else's.

        Oldest first, and capped, because the case this exists for is a queue tens of thousands
        deep: a sweep that drains the worst of it every tick beats one that reads all of it once.
        """
        ...

    async def apply_transition(self, transition: JobTransition) -> JobRecord:
        """The only write to `jobs` in the system (D066). Returns the row as it now stands."""
        ...


@runtime_checkable
class RequestStore(Protocol):
    """The `requests` table, read side.

    Written once inside `UnitOfWork.commit_submission` and never updated (override item 4), so
    there is deliberately no write method here to update it with.
    """

    async def load(self, request_key: RequestKey) -> StoredRequest | None:
        """The body as it arrived. No scope: the runner reads it, not a caller."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """One transaction, one shape.

    The whole port is a single method taking a single value, which is what makes "these three
    rows land together or not at all" a property of the type rather than of a code review.
    """

    async def commit_submission(self, submission: Submission) -> None:
        """Insert the request, the job and the work item in one transaction."""
        ...


@runtime_checkable
class WorkQueue(Protocol):
    """`work_items`, claimed with `FOR UPDATE SKIP LOCKED` under a lease."""

    async def claim(self, owner: str, lease_seconds: int) -> ClaimedWorkItem | None:
        """Take the oldest available row, or `None` when the queue is empty."""
        ...

    async def heartbeat(
        self, item_id: WorkItemId, owner: str, lease_seconds: int
    ) -> ClaimedWorkItem | None:
        """Push `claimed_until` out by another lease. `None` means the claim is no longer ours."""
        ...

    async def release(self, item_id: WorkItemId, owner: str) -> None:
        """Hand a claim back untouched, so the row is immediately claimable again.

        Called on a clean stop between claiming and starting, and on no other path. A process
        that dies must NOT reach this: an abandoned lease has to expire so the sweep can find it.
        """
        ...

    async def complete(self, item_id: WorkItemId, owner: str) -> None:
        """Delete the row. The job is terminal and the demo never retries it (docs/demo.md D2)."""
        ...

    async def reclaim(self, now: datetime, max_claims: int) -> int:
        """Hand every lapsed claim back. Returns the rows made claimable again.

        Clears `claimed_by` and `claimed_until` and increments `claim_count` on every row with
        `claimed_until <= now`, so the next worker to poll can have it. A row that has already
        reached `max_claims` (`config.work_max_claims`) is left exactly where it is: handing it
        back a fourth time is how a poison item eats a queue, and `exhausted` is where it goes
        instead.

        `work_items` and nothing else. `workflow_runs` is not created in migration 1 (D093), and
        the three columns this needs are already there.
        """
        ...

    async def exhausted(self, now: datetime, max_claims: int) -> tuple[ClaimedWorkItem, ...]:
        """Lapsed rows that have been handed back `max_claims` times: what `reclaim` will not.

        Separate from `reclaim` because the two outcomes are written by different owners. Giving
        a row back is the queue's business; giving up on the job it points at is orchestration's,
        and only `JobRepository.apply_transition` can do it (D066).

        `now` is carried so a row on its last claim that a live worker is still holding is not in
        this set. Its lease has not lapsed, so it has not failed yet.
        """
        ...

    async def discard(self, item_id: WorkItemId) -> bool:
        """Delete a row whoever holds it. `True` when a row went.

        `complete` needs an owner to match and a swept row has none worth naming: it is either
        unclaimed or held by a process that is not coming back. A `FAILED` job whose queue row
        outlived it is a row the next worker claims and the runner then drops, forever.
        """
        ...


@runtime_checkable
class BriefWriter(Protocol):
    """`intake`, from the outside. Sole writer of `briefs` (D066)."""

    async def seal(
        self,
        *,
        job_id: JobId,
        request: StoredRequest,
        constraints: JobConstraints,
        profile: ProfileId,
    ) -> BriefRecord:
        """Sanitise the stored body, seal the brief, insert the row, return it.

        Orchestration hands over the raw request and the effective constraints and gets back a
        sealed record. What sanitising means, what the guard verdict looks like and which
        template rendered it are intake's business, and none of it is visible here.
        """
        ...


@runtime_checkable
class GenerationGateway(Protocol):
    """`generation`, from the outside: the ACL over the external agent worker.

    Writes nothing. It is handed a sealed brief and a session identity and returns claims.
    """

    async def generate(
        self,
        *,
        session_id: SessionId,
        trace_id: TraceId,
        brief: BriefRecord,
        constraints: JobConstraints,
        profile: ProfileId,
    ) -> GenerationOutcome:
        """Run one generation attempt under a session identity the worker cannot invert.

        `session_id` and `trace_id` are the only ids that cross (`plan/12-data-control.md`).
        The job id, the principal and the chat context do not, and there is nothing in the
        payload to address them with.
        """
        ...


@runtime_checkable
class ArtifactWriter(Protocol):
    """`custody`, from the outside. Sole writer of `artifacts` and of object storage (D066)."""

    async def harvest(
        self,
        *,
        job_id: JobId,
        principal_id: PrincipalId,
        chat_context_id: ChatContextId | None,
        profile: ProfileId,
        outcome: GenerationOutcome,
    ) -> HarvestOutcome:
        """Fetch the claimed bytes, verify them, store them, insert the rows.

        `principal_id` and `chat_context_id` are passed in because custody copies them onto every
        artifact row at insert (A6/D088 and D073) rather than deriving them at read time. Custody
        gets no `JobRecord`: it has no business reading a status, and it cannot write one.
        """
        ...

    async def publish(self, artifact_id: ArtifactId) -> ArtifactRecord:
        """Stamp `published_at` on the primary. Custody's own column, custody's own write."""
        ...


@runtime_checkable
class ArtifactReader(Protocol):
    """The only artifact read path the API is allowed to reach, through a use case."""

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        """`LEARNER` audience and `CLEAN` verdict only; the index carries both predicates.

        @audit no ownership check. Any caller lists any artifact (scope override item 1).
        """
        ...

    async def open_content(
        self, scope: AccessScope, artifact_id: ArtifactId
    ) -> ContentStream | None:
        """Open the bytes for streaming. `None` when there is no such readable artifact.

        @audit no ownership check. Any caller streams any artifact.
        """
        ...
