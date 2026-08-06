"""Row builders every storage test shares, and the check that an adapter still fits its port.

Ids are derived from fixed bytes rather than minted, so `job_id_for(3)` is the same string on
every run and in every backend. That is what lets a contract test assert on an order -- two rows
written in the same microsecond are separated by their ids, and a test that could not name those
ids in advance could only assert that the page had two things in it.

`assert_conforms` compares an adapter's methods against the port's by name, by parameter name and
by parameter kind. `isinstance` against a `Protocol` checks the names and stops there, so a
double whose `list` had grown an extra keyword would pass it and fail against the real caller.
"""

import inspect
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from app.domain.access import AccessScope
from app.domain.enums import (
    ArtifactRole,
    Audience,
    ContextKind,
    JobStatus,
    ProfileId,
    ScanVerdict,
    StageName,
    percent_for,
)
from app.domain.ids import (
    ArtifactId,
    BriefId,
    JobId,
    PrincipalId,
    RequestKey,
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
    ContextItem,
    JobConstraints,
    JobRecord,
    StoredRequest,
)
from app.orchestration.ports import QueuedWorkItem, Submission

T0: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
PRINCIPAL: Final[PrincipalId] = "u_demo"
OTHER_PRINCIPAL: Final[PrincipalId] = "u_someone_else"
OWNER: Final[str] = "worker-1"
OTHER_OWNER: Final[str] = "worker-2"
LEASE_SECONDS: Final[int] = 60


def request_key_for(index: int) -> RequestKey:
    """A distinct request key per index, from fixed bytes so ids are stable across runs."""
    return mint_request_key(bytes([index]) * 16)


def job_id_for(index: int) -> JobId:
    return derive_job_id(request_key_for(index))


def work_item_id_for(index: int) -> WorkItemId:
    return derive_work_item_id(job_id_for(index))


def brief_id_for(index: int) -> BriefId:
    return derive_brief_id(job_id_for(index))


def content_hash_for(index: int) -> str:
    return "sha256:" + f"{index:02x}" * 32


def artifact_id_for(index: int) -> ArtifactId:
    return derive_artifact_id(job_id_for(index), content_hash_for(index))


def demo_scope(principal_id: PrincipalId = PRINCIPAL) -> AccessScope:
    """The permissive scope the demo mints for every caller (scope override item 1)."""
    return AccessScope._mint(principal_id)


def constraints() -> JobConstraints:
    return JobConstraints(max_duration_s=90, language="en", reading_level=None)


def make_request(index: int = 0, *, principal_id: PrincipalId = PRINCIPAL) -> StoredRequest:
    raw: Mapping[str, object] = {"instruction": "why do atoms form covalent bonds"}
    return StoredRequest(
        request_key=request_key_for(index),
        principal_id=principal_id,
        raw=raw,
        received_at=T0,
    )


def make_job(
    index: int = 0,
    *,
    principal_id: PrincipalId = PRINCIPAL,
    status: JobStatus = JobStatus.QUEUED,
    stage: StageName = StageName.INTAKE,
    version: int = 0,
    created_at: datetime = T0,
) -> JobRecord:
    return JobRecord(
        job_id=job_id_for(index),
        request_key=request_key_for(index),
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


def make_queued_item(
    index: int = 0,
    *,
    available_at: datetime = T0,
    created_at: datetime = T0,
) -> QueuedWorkItem:
    return QueuedWorkItem(
        item_id=work_item_id_for(index),
        job_id=job_id_for(index),
        available_at=available_at,
        created_at=created_at,
    )


def make_submission(
    index: int = 0,
    *,
    principal_id: PrincipalId = PRINCIPAL,
    created_at: datetime = T0,
    job: JobRecord | None = None,
) -> Submission:
    """The three rows one submit writes.

    `job` overrides only the middle row, which is how a test builds a submission whose second
    insert is the one that fails: a fresh request key carrying a job id that is already taken.
    """
    return Submission(
        request=make_request(index, principal_id=principal_id),
        job=(
            make_job(index, principal_id=principal_id, created_at=created_at)
            if job is None
            else job
        ),
        work_item=make_queued_item(index, available_at=created_at, created_at=created_at),
    )


def make_brief(index: int = 0, *, subject: str = "chemistry") -> BriefRecord:
    return BriefRecord(
        brief_id=brief_id_for(index),
        job_id=job_id_for(index),
        brief_hash="sha256:" + "ab" * 32,
        template_version="v1",
        subject=subject,
        concept_id=None,
        instruction="why do atoms form covalent bonds",
        context_items=(ContextItem(kind=ContextKind.LEVEL, text="grade 9"),),
        constraints=constraints(),
        guard_verdict={"decision": "ALLOW", "guard_version": "permissive.v0"},
        sealed_at=T0,
    )


def make_artifact(
    index: int = 0,
    *,
    job_index: int | None = None,
    principal_id: PrincipalId = PRINCIPAL,
    audience: Audience = Audience.LEARNER,
    verdict: ScanVerdict = ScanVerdict.CLEAN,
    created_at: datetime = T0,
) -> ArtifactRecord:
    job_id = job_id_for(index if job_index is None else job_index)
    return ArtifactRecord(
        artifact_id=artifact_id_for(index),
        job_id=job_id,
        principal_id=principal_id,
        chat_context_id=None,
        role=ArtifactRole.PRIMARY,
        audience=audience,
        mime="video/mp4",
        rel_path=None,
        size_bytes=1024,
        content_hash=content_hash_for(index),
        storage_uri=f"memory://artifacts/{job_id}/lesson.mp4",
        probe={},
        scan_verdict=verdict,
        validator_version="v1",
        published_at=created_at,
        created_at=created_at,
    )


def _protocol_methods(protocol: type) -> dict[str, inspect.Signature]:
    """The methods a protocol declares, ignoring the machinery `Protocol` adds."""
    return {
        name: inspect.signature(member)
        for name, member in vars(protocol).items()
        if not name.startswith("_") and callable(member)
    }


def assert_conforms(adapter: object, protocol: type) -> None:
    """An adapter implements a port only if the parameters line up, not just the names."""
    assert isinstance(adapter, protocol), (
        f"{type(adapter).__name__} misses a {protocol.__name__} method"
    )
    for name, declared in _protocol_methods(protocol).items():
        actual = inspect.signature(getattr(type(adapter), name))
        assert [p.name for p in actual.parameters.values()] == [
            p.name for p in declared.parameters.values()
        ], f"{type(adapter).__name__}.{name} parameter names"
        assert [p.kind for p in actual.parameters.values()] == [
            p.kind for p in declared.parameters.values()
        ], f"{type(adapter).__name__}.{name} parameter kinds"
