"""Fakes for every orchestration port, plus the conformance checks that keep them honest.

A fake that has drifted from its `Protocol` is a green test against a shape nobody implements,
so the fakes live next to the check rather than anywhere convenient: `assert_conforms` compares
a fake's methods against the protocol's by name, by parameter name, and by parameter kind, and
every fake below is run through it.

This module is named `*_test.py` on purpose. The orchestration lane owns
`tests/unit/orchestration/*_test.py` and nothing else in that directory, so the shared fakes
have to be a test module; the other test modules import them from here.

Nothing here imports an adapter. `app.storage` does not exist for this lane and the
import-linter contract says it never will.
"""

import inspect
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from app.domain.access import AccessScope
from app.domain.enums import (
    ArtifactRole,
    Audience,
    JobStatus,
    ProfileId,
    ScanVerdict,
    StageName,
    percent_for,
)
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
    derive_artifact_id,
    derive_brief_id,
    derive_job_id,
    derive_work_item_id,
    mint_request_key,
)
from app.domain.records import (
    ArtifactRecord,
    BriefRecord,
    ClaimedWorkItem,
    ContentStream,
    Cursor,
    JobConstraints,
    JobRecord,
    Page,
    StoredRequest,
)
from app.orchestration.engine.policy import lease_expired
from app.orchestration.ports import (
    AdmissionPolicy,
    ArtifactDescriptor,
    ArtifactReader,
    ArtifactWriter,
    BriefWriter,
    Clock,
    GenerationGateway,
    GenerationOutcome,
    HarvestOutcome,
    JobRepository,
    JobTransition,
    QueuedWorkItem,
    RequestStore,
    Submission,
    UnitOfWork,
    WorkQueue,
)

# --------------------------------------------------------------------------- fixed values

T0: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
PRINCIPAL: Final[PrincipalId] = "u_demo"
OTHER_PRINCIPAL: Final[PrincipalId] = "u_someone_else"
REQUEST_KEY: Final[RequestKey] = mint_request_key(bytes(range(16)))
JOB_ID: Final[JobId] = derive_job_id(REQUEST_KEY)
WORK_ITEM_ID: Final[WorkItemId] = derive_work_item_id(JOB_ID)
BRIEF_ID: Final[BriefId] = derive_brief_id(JOB_ID)
CONTENT_HASH: Final[str] = "sha256:" + "ab" * 32
ARTIFACT_ID: Final[ArtifactId] = derive_artifact_id(JOB_ID, CONTENT_HASH)
SESSION_ID: Final[SessionId] = "ses_" + "0" * 26
TRACE_ID: Final[TraceId] = "tr_" + "0" * 26
OWNER: Final[str] = "worker-1"
LEASE_SECONDS: Final[int] = 60


def demo_scope(principal_id: PrincipalId = PRINCIPAL) -> AccessScope:
    """The permissive scope the demo mints for every caller (scope override item 1)."""
    return AccessScope._mint(principal_id)


def constraints() -> JobConstraints:
    return JobConstraints(max_duration_s=90, language="en", reading_level=None)


def make_job(
    *,
    job_id: JobId = JOB_ID,
    principal_id: PrincipalId = PRINCIPAL,
    status: JobStatus = JobStatus.QUEUED,
    stage: StageName = StageName.INTAKE,
    version: int = 0,
    created_at: datetime = T0,
) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        request_key=REQUEST_KEY,
        principal_id=principal_id,
        chat_context_id=None,
        status=status,
        stage=stage,
        attempt=0,
        progress_percent=percent_for(stage),
        profile=ProfileId.VIDEO_SHORT_V1,
        contract_version="v1",
        constraints=constraints(),
        failure=None,
        artifact_id=None,
        brief_id=None,
        version=version,
        created_at=created_at,
        updated_at=created_at,
    )


def make_request(raw: Mapping[str, object] | None = None) -> StoredRequest:
    body: Mapping[str, object] = {"instruction": "why do atoms form covalent bonds"}
    return StoredRequest(
        request_key=REQUEST_KEY,
        principal_id=PRINCIPAL,
        raw=body if raw is None else raw,
        received_at=T0,
    )


def make_brief() -> BriefRecord:
    return BriefRecord(
        brief_id=BRIEF_ID,
        job_id=JOB_ID,
        brief_hash="sha256:" + "cd" * 32,
        template_version="v1",
        subject="chemistry",
        concept_id=None,
        instruction="why do atoms form covalent bonds",
        context_items=(),
        constraints=constraints(),
        guard_verdict={},
        sealed_at=T0,
    )


def make_artifact(
    *, artifact_id: ArtifactId = ARTIFACT_ID, job_id: JobId = JOB_ID, published: bool = False
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        job_id=job_id,
        principal_id=PRINCIPAL,
        chat_context_id=None,
        role=ArtifactRole.PRIMARY,
        audience=Audience.LEARNER,
        mime="video/mp4",
        rel_path=None,
        size_bytes=1024,
        content_hash=CONTENT_HASH,
        storage_uri="s3://artifacts/" + artifact_id,
        probe={},
        scan_verdict=ScanVerdict.CLEAN,
        validator_version="v1",
        published_at=T0 if published else None,
        created_at=T0,
    )


def make_descriptor() -> ArtifactDescriptor:
    return ArtifactDescriptor(
        role=ArtifactRole.PRIMARY,
        media_type="video/mp4",
        size_bytes=1024,
        source_uri="workspace://out/lesson.mp4",
    )


def make_outcome() -> GenerationOutcome:
    return GenerationOutcome(session_id=SESSION_ID, descriptors=(make_descriptor(),))


def make_claimed_item(*, claimed_until: datetime | None = None) -> ClaimedWorkItem:
    return ClaimedWorkItem(
        item_id=WORK_ITEM_ID,
        job_id=JOB_ID,
        claimed_by=OWNER,
        claimed_until=T0 + timedelta(seconds=LEASE_SECONDS)
        if claimed_until is None
        else claimed_until,
        claim_count=1,
    )


# --------------------------------------------------------------------------- fakes


@dataclass(slots=True)
class FrozenClock:
    """A `Clock` that only moves when a test says so."""

    at: datetime = T0

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)


@dataclass(slots=True)
class FakeJobRepository:
    """In-memory `JobRepository`. Records every transition so a test can read the whole run."""

    clock: FrozenClock = field(default_factory=FrozenClock)
    rows: dict[JobId, JobRecord] = field(default_factory=dict)
    transitions: list[JobTransition] = field(default_factory=list)
    history: list[JobRecord] = field(default_factory=list)
    active_count: int = 0

    def seed(self, job: JobRecord) -> JobRecord:
        self.rows[job.job_id] = job
        return job

    async def count_active(self, scope: AccessScope, principal_id: PrincipalId) -> int:
        del scope
        seeded = sum(
            1
            for job in self.rows.values()
            if job.principal_id == principal_id
            and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        )
        return max(self.active_count, seeded)

    async def get(self, scope: AccessScope, job_id: JobId) -> JobRecord | None:
        del scope  # no ownership predicate, by design
        return self.rows.get(job_id)

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        del scope  # no ownership predicate, by design
        ordered = sorted(
            self.rows.values(), key=lambda job: (job.created_at, job.job_id), reverse=True
        )
        if cursor is not None:
            ordered = [
                job
                for job in ordered
                if (job.created_at, job.job_id) < (cursor.created_at, cursor.id)
            ]
        page = ordered[:limit]
        next_cursor = (
            Cursor(created_at=page[-1].created_at, id=page[-1].job_id).encode()
            if len(ordered) > limit and page
            else None
        )
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def load_for_run(self, job_id: JobId) -> JobRecord | None:
        return self.rows.get(job_id)

    async def list_untouched_since(
        self, status: JobStatus, moment: datetime, *, limit: int
    ) -> tuple[JobRecord, ...]:
        ordered = sorted(
            (
                job
                for job in self.rows.values()
                if job.status is status and job.updated_at <= moment
            ),
            key=lambda job: (job.updated_at, job.job_id),
        )
        return tuple(ordered[:limit])

    async def apply_transition(self, transition: JobTransition) -> JobRecord:
        self.transitions.append(transition)
        current = self.rows[transition.job_id]
        assert current.version == transition.expected_version, "optimistic concurrency"
        updated = replace(
            current,
            status=transition.status,
            stage=transition.stage,
            progress_percent=transition.progress_percent,
            brief_id=transition.brief_id if transition.brief_id is not None else current.brief_id,
            artifact_id=(
                transition.artifact_id
                if transition.artifact_id is not None
                else current.artifact_id
            ),
            failure=transition.failure if transition.failure is not None else current.failure,
            version=current.version + 1,
            updated_at=self.clock.now(),
        )
        self.rows[updated.job_id] = updated
        self.history.append(updated)
        return updated


@dataclass(slots=True)
class FakeRequestStore:
    rows: dict[RequestKey, StoredRequest] = field(default_factory=dict)

    def seed(self, request: StoredRequest) -> StoredRequest:
        self.rows[request.request_key] = request
        return request

    async def load(self, request_key: RequestKey) -> StoredRequest | None:
        return self.rows.get(request_key)


class UnitOfWorkFailure(RuntimeError):
    """What a fake transaction raises when it is told to fail on commit."""


@dataclass(slots=True)
class FakeUnitOfWork:
    """Atomic by construction: either all three rows land or the store is untouched."""

    fail_on_commit: bool = False
    requests: dict[RequestKey, StoredRequest] = field(default_factory=dict)
    jobs: dict[JobId, JobRecord] = field(default_factory=dict)
    work_items: dict[WorkItemId, QueuedWorkItem] = field(default_factory=dict)
    commits: int = 0

    async def commit_submission(self, submission: Submission) -> None:
        self.commits += 1
        if self.fail_on_commit:
            raise UnitOfWorkFailure("transaction rolled back")
        self.requests[submission.request.request_key] = submission.request
        self.jobs[submission.job.job_id] = submission.job
        self.work_items[submission.work_item.item_id] = submission.work_item

    def is_empty(self) -> bool:
        return not (self.requests or self.jobs or self.work_items)


@dataclass(slots=True)
class FakeWorkQueue:
    """`work_items` in a list, with the lease arithmetic the sweep reads.

    `counts` is `work_items.claim_count` per row: how many times the sweep has taken this row off
    a holder that never came back. A fresh row is at zero, and `reclaim` is the only thing that
    moves it, which is what makes `work_max_claims` a count of dead workers rather than of runs.
    """

    clock: FrozenClock = field(default_factory=FrozenClock)
    available: list[QueuedWorkItem] = field(default_factory=list)
    claimed: dict[WorkItemId, ClaimedWorkItem] = field(default_factory=dict)
    completed: list[WorkItemId] = field(default_factory=list)
    released: list[WorkItemId] = field(default_factory=list)
    discarded: list[WorkItemId] = field(default_factory=list)
    counts: dict[WorkItemId, int] = field(default_factory=dict)
    heartbeats: list[tuple[WorkItemId, datetime]] = field(default_factory=list)
    heartbeat_returns_none_after: int | None = None
    claim_count: int = 0
    sweep_error: Exception | None = None
    """Raised by the two reads a sweep tick makes, so a test can break one tick and no more."""

    def seed(self, item: QueuedWorkItem) -> QueuedWorkItem:
        self.available.append(item)
        return item

    def seed_claimed(self, item: ClaimedWorkItem) -> ClaimedWorkItem:
        """Put a row in the state a dead worker leaves behind: claimed, with a lease to lapse."""
        self.claimed[item.item_id] = item
        self.counts[item.item_id] = item.claim_count
        return item

    async def claim(self, owner: str, lease_seconds: int) -> ClaimedWorkItem | None:
        if not self.available:
            return None
        item = self.available.pop(0)
        self.claim_count += 1
        claimed = ClaimedWorkItem(
            item_id=item.item_id,
            job_id=item.job_id,
            claimed_by=owner,
            claimed_until=self.clock.now() + timedelta(seconds=lease_seconds),
            claim_count=self.counts.get(item.item_id, 0),
        )
        self.claimed[item.item_id] = claimed
        return claimed

    async def heartbeat(
        self, item_id: WorkItemId, owner: str, lease_seconds: int
    ) -> ClaimedWorkItem | None:
        if (
            self.heartbeat_returns_none_after is not None
            and len(self.heartbeats) >= self.heartbeat_returns_none_after
        ):
            return None
        deadline = self.clock.now() + timedelta(seconds=lease_seconds)
        self.heartbeats.append((item_id, deadline))
        held = self.claimed[item_id]
        renewed = ClaimedWorkItem(
            item_id=held.item_id,
            job_id=held.job_id,
            claimed_by=owner,
            claimed_until=deadline,
            claim_count=held.claim_count,
        )
        self.claimed[item_id] = renewed
        return renewed

    async def release(self, item_id: WorkItemId, owner: str) -> None:
        del owner
        held = self.claimed.pop(item_id, None)
        self.released.append(item_id)
        if held is not None:
            self.available.append(
                QueuedWorkItem(
                    item_id=held.item_id,
                    job_id=held.job_id,
                    available_at=self.clock.now(),
                    created_at=self.clock.now(),
                )
            )

    async def complete(self, item_id: WorkItemId, owner: str) -> None:
        del owner
        self.claimed.pop(item_id, None)
        self.completed.append(item_id)

    async def reclaim(self, now: datetime, max_claims: int) -> int:
        if self.sweep_error is not None:
            raise self.sweep_error
        handed_back = 0
        for item_id, held in list(self.claimed.items()):
            if not lease_expired(held.claimed_until, now):
                continue
            if held.claim_count >= max_claims:
                continue
            del self.claimed[item_id]
            self.counts[item_id] = held.claim_count + 1
            self.available.append(
                QueuedWorkItem(
                    item_id=held.item_id,
                    job_id=held.job_id,
                    available_at=now,
                    created_at=now,
                )
            )
            handed_back += 1
        return handed_back

    async def exhausted(self, now: datetime, max_claims: int) -> tuple[ClaimedWorkItem, ...]:
        if self.sweep_error is not None:
            raise self.sweep_error
        return tuple(
            held
            for held in self.claimed.values()
            if lease_expired(held.claimed_until, now) and held.claim_count >= max_claims
        )

    async def discard(self, item_id: WorkItemId) -> bool:
        held = self.claimed.pop(item_id, None)
        waiting = [item for item in self.available if item.item_id == item_id]
        for item in waiting:
            self.available.remove(item)
        self.discarded.append(item_id)
        return held is not None or bool(waiting)


@dataclass(slots=True)
class FakeBriefWriter:
    brief: BriefRecord = field(default_factory=make_brief)
    raises: Exception | None = None
    calls: list[JobId] = field(default_factory=list)

    async def seal(
        self,
        *,
        job_id: JobId,
        request: StoredRequest,
        constraints: JobConstraints,
        profile: ProfileId,
    ) -> BriefRecord:
        del request, constraints, profile
        self.calls.append(job_id)
        if self.raises is not None:
            raise self.raises
        return self.brief


@dataclass(slots=True)
class FakeGenerationGateway:
    outcome: GenerationOutcome = field(default_factory=make_outcome)
    raises: Exception | None = None
    calls: list[SessionId] = field(default_factory=list)

    async def generate(
        self,
        *,
        session_id: SessionId,
        trace_id: TraceId,
        brief: BriefRecord,
        constraints: JobConstraints,
        profile: ProfileId,
    ) -> GenerationOutcome:
        del trace_id, brief, constraints, profile
        self.calls.append(session_id)
        if self.raises is not None:
            raise self.raises
        return self.outcome


@dataclass(slots=True)
class FakeArtifactWriter:
    record: ArtifactRecord = field(default_factory=make_artifact)
    raises: Exception | None = None
    harvested: list[JobId] = field(default_factory=list)
    published: list[ArtifactId] = field(default_factory=list)

    async def harvest(
        self,
        *,
        job_id: JobId,
        principal_id: PrincipalId,
        chat_context_id: ChatContextId | None,
        profile: ProfileId,
        outcome: GenerationOutcome,
    ) -> HarvestOutcome:
        del principal_id, chat_context_id, profile, outcome
        self.harvested.append(job_id)
        if self.raises is not None:
            raise self.raises
        return HarvestOutcome(primary=self.record, artifacts=(self.record,))

    async def publish(self, artifact_id: ArtifactId) -> ArtifactRecord:
        self.published.append(artifact_id)
        return make_artifact(artifact_id=artifact_id, published=True)


async def _one_chunk() -> AsyncIterator[bytes]:
    yield b"mp4"


@dataclass(slots=True)
class FakeArtifactReader:
    rows: dict[ArtifactId, ArtifactRecord] = field(default_factory=dict)
    opened: list[ArtifactId] = field(default_factory=list)

    def seed(self, artifact: ArtifactRecord) -> ArtifactRecord:
        self.rows[artifact.artifact_id] = artifact
        return artifact

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        del scope  # no ownership predicate, by design
        ordered = sorted(
            self.rows.values(),
            key=lambda row: (row.created_at, row.artifact_id),
            reverse=True,
        )
        if job_id is not None:
            ordered = [row for row in ordered if row.job_id == job_id]
        if cursor is not None:
            ordered = [
                row
                for row in ordered
                if (row.created_at, row.artifact_id) < (cursor.created_at, cursor.id)
            ]
        page = ordered[:limit]
        next_cursor = (
            Cursor(created_at=page[-1].created_at, id=page[-1].artifact_id).encode()
            if len(ordered) > limit and page
            else None
        )
        return Page(items=tuple(page), next_cursor=next_cursor)

    async def open_content(
        self, scope: AccessScope, artifact_id: ArtifactId
    ) -> ContentStream | None:
        del scope  # no ownership predicate, by design
        self.opened.append(artifact_id)
        row = self.rows.get(artifact_id)
        if row is None:
            return None
        return ContentStream(
            media_type=row.mime,
            size_bytes=row.size_bytes,
            content_hash=row.content_hash,
            filename="lesson.mp4",
            disposition="inline",
            chunks=_one_chunk(),
        )


@dataclass(slots=True)
class RefusingAdmission:
    """An `AdmissionPolicy` that always refuses, so a test can prove submit stops at the gate."""

    error: Exception
    calls: int = 0

    async def admit(self, scope: AccessScope) -> None:
        del scope
        self.calls += 1
        raise self.error


@dataclass(slots=True)
class AdmittingAdmission:
    calls: int = 0

    async def admit(self, scope: AccessScope) -> None:
        del scope
        self.calls += 1


# --------------------------------------------------------------------------- conformance


def _protocol_methods(protocol: type) -> dict[str, inspect.Signature]:
    """The methods a protocol declares, ignoring the machinery `Protocol` adds."""
    return {
        name: inspect.signature(member)
        for name, member in vars(protocol).items()
        if not name.startswith("_") and callable(member)
    }


def assert_conforms(fake: object, protocol: type) -> None:
    """A fake implements a port only if the parameters line up, not just the names."""
    assert isinstance(fake, protocol), f"{type(fake).__name__} misses a {protocol.__name__} method"
    for name, declared in _protocol_methods(protocol).items():
        actual = inspect.signature(getattr(type(fake), name))
        assert [p.name for p in actual.parameters.values()] == [
            p.name for p in declared.parameters.values()
        ], f"{type(fake).__name__}.{name} parameter names"
        assert [p.kind for p in actual.parameters.values()] == [
            p.kind for p in declared.parameters.values()
        ], f"{type(fake).__name__}.{name} parameter kinds"


def test_every_fake_conforms_to_its_port() -> None:
    assert_conforms(FrozenClock(), Clock)
    assert_conforms(FakeJobRepository(), JobRepository)
    assert_conforms(FakeRequestStore(), RequestStore)
    assert_conforms(FakeUnitOfWork(), UnitOfWork)
    assert_conforms(FakeWorkQueue(), WorkQueue)
    assert_conforms(FakeBriefWriter(), BriefWriter)
    assert_conforms(FakeGenerationGateway(), GenerationGateway)
    assert_conforms(FakeArtifactWriter(), ArtifactWriter)
    assert_conforms(FakeArtifactReader(), ArtifactReader)
    assert_conforms(AdmittingAdmission(), AdmissionPolicy)


def test_a_fake_missing_a_method_is_caught() -> None:
    """The conformance helper has to be able to fail, or it proves nothing."""

    class Hollow:
        pass

    try:
        assert_conforms(Hollow(), JobRepository)
    except AssertionError:
        return
    raise AssertionError("assert_conforms accepted a class with no methods")
