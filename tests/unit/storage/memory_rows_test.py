"""Row builders for the storage doubles, and the check that they still fit their ports.

A double that has drifted from its `Protocol` is a green suite against a shape nothing
implements, so the conformance check lives beside the builders every other module here imports:
`assert_conforms` compares each double's methods against the port's by name, by parameter name
and by parameter kind.

This module is named `*_test.py` because the storage lane owns `tests/unit/storage/*_test.py`
and nothing else in that directory. The sibling test modules import their rows from here.
"""

import inspect
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from app.access.ports import PrincipalRepository
from app.api.ports import DatabaseProbe
from app.custody.ports import ArtifactStore, ArtifactWriter
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
    JobId,
    PrincipalId,
    RequestKey,
    WorkItemId,
    derive_artifact_id,
    derive_job_id,
    derive_work_item_id,
    mint_request_key,
)
from app.domain.records import ArtifactRecord, JobConstraints, JobRecord, StoredRequest
from app.orchestration.ports import (
    Clock,
    JobRepository,
    QueuedWorkItem,
    RequestStore,
    Submission,
    UnitOfWork,
    WorkQueue,
)
from app.storage.memory import (
    FrozenClock,
    MemoryArtifactRepository,
    MemoryDatabase,
    MemoryDatabaseProbe,
    MemoryJobRepository,
    MemoryObjectStore,
    MemoryPrincipalRepository,
    MemoryRequestStore,
    MemoryUnitOfWork,
    MemoryWorkQueue,
)
from app.storage.objects import FilesystemObjectStore, ObjectStore, S3ObjectStore

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
) -> Submission:
    return Submission(
        request=make_request(index, principal_id=principal_id),
        job=make_job(index, principal_id=principal_id, created_at=created_at),
        work_item=make_queued_item(index, available_at=created_at, created_at=created_at),
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


def assert_conforms(double: object, protocol: type) -> None:
    """A double implements a port only if the parameters line up, not just the names."""
    assert isinstance(double, protocol), (
        f"{type(double).__name__} misses a {protocol.__name__} method"
    )
    for name, declared in _protocol_methods(protocol).items():
        actual = inspect.signature(getattr(type(double), name))
        assert [p.name for p in actual.parameters.values()] == [
            p.name for p in declared.parameters.values()
        ], f"{type(double).__name__}.{name} parameter names"
        assert [p.kind for p in actual.parameters.values()] == [
            p.kind for p in declared.parameters.values()
        ], f"{type(double).__name__}.{name} parameter kinds"


def test_every_double_conforms_to_its_port() -> None:
    database = MemoryDatabase()
    clock = FrozenClock(at=T0)

    assert_conforms(clock, Clock)
    assert_conforms(MemoryRequestStore(database), RequestStore)
    assert_conforms(MemoryJobRepository(database, clock), JobRepository)
    assert_conforms(MemoryUnitOfWork(database), UnitOfWork)
    assert_conforms(MemoryWorkQueue(database, clock), WorkQueue)
    assert_conforms(MemoryPrincipalRepository(database, clock), PrincipalRepository)
    assert_conforms(MemoryArtifactRepository(database), ArtifactWriter)
    assert_conforms(MemoryDatabaseProbe(database), DatabaseProbe)
    assert_conforms(MemoryObjectStore(), ObjectStore)
    assert_conforms(MemoryObjectStore(), ArtifactStore)


def test_both_object_backends_conform_to_the_same_two_ports() -> None:
    """The stub is held to the port as strictly as the backend that works.

    A declared stub whose signatures had drifted would be discovered on the day somebody filled
    in its bodies, which is the worst possible day to discover it.
    """
    filesystem = FilesystemObjectStore(Path("unused"), bucket="artifacts")
    s3 = S3ObjectStore(bucket="artifacts")

    for store in (filesystem, s3):
        assert_conforms(store, ObjectStore)
        assert_conforms(store, ArtifactStore)


def test_a_double_missing_a_method_is_caught() -> None:
    """The conformance helper has to be able to fail, or it proves nothing."""

    class Hollow:
        pass

    try:
        assert_conforms(Hollow(), JobRepository)
    except AssertionError:
        return
    raise AssertionError("assert_conforms accepted a class with no methods")
