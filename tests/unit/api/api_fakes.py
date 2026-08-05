"""Fakes and record builders for the HTTP edge.

The routers are tested against hand-written fakes of the two service ports, never against
another lane's adapter. That is the point of `app/api/ports.py`: the edge is finished and
testable before orchestration or storage exist, and a fake that drifts from the port stops
satisfying `isinstance(..., JobService)` in `service_ports_test.py`.

Not a test module and not a conftest: the test files next to it import it by name, which works
because pytest puts this directory on `sys.path`. The name carries the `api_` prefix so it
cannot collide with another lane's helper module of the same purpose.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Final

from app.domain.access import AccessScope, Principal
from app.domain.enums import (
    STAGE_PERCENT,
    ArtifactRole,
    Audience,
    JobStatus,
    ProfileId,
    ScanVerdict,
    StageName,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import (
    PrincipalId,
    derive_artifact_id,
    derive_job_id,
    mint_request_key,
    new_request_key,
)
from app.domain.records import (
    ArtifactRecord,
    ContentStream,
    Cursor,
    JobConstraints,
    JobRecord,
    Page,
    SubmitJobCommand,
)

DEFAULT_PRINCIPAL: Final[str] = "u_demo"
CREATED_AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
UPDATED_AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 41, tzinfo=UTC)
FIXED_ENTROPY: Final[bytes] = bytes(range(16))
FIXED_REQUEST_KEY: Final[str] = mint_request_key(FIXED_ENTROPY)
FIXED_JOB_ID: Final[str] = derive_job_id(FIXED_REQUEST_KEY)
FIXED_CONTENT_HASH: Final[str] = "sha256:" + "ab" * 32
FIXED_ARTIFACT_ID: Final[str] = derive_artifact_id(FIXED_JOB_ID, FIXED_CONTENT_HASH)
FIXED_TRACE_ID: Final[str] = "tr_" + "0" * 26
SAMPLE_BYTES: Final[tuple[bytes, ...]] = (b"\x00\x00\x00\x18ftypmp42", b"payload")
SAMPLE_SIZE: Final[int] = sum(len(chunk) for chunk in SAMPLE_BYTES)


# --------------------------------------------------------------------------- record builders


def make_job(
    *,
    job_id: str = FIXED_JOB_ID,
    request_key: str = FIXED_REQUEST_KEY,
    principal_id: str = DEFAULT_PRINCIPAL,
    status: JobStatus = JobStatus.QUEUED,
    stage: StageName = StageName.INTAKE,
    version: int = 0,
    artifact_id: str | None = None,
    created_at: datetime = CREATED_AT,
) -> JobRecord:
    """A `jobs` row as orchestration would hand one back."""
    return JobRecord(
        job_id=job_id,
        request_key=request_key,
        principal_id=principal_id,
        chat_context_id=None,
        status=status,
        stage=stage,
        attempt=0,
        # A FAILED job keeps whatever the bar last showed, so the map is read with a floor
        # rather than through `percent_for`, which refuses a stage the demo never scores.
        progress_percent=STAGE_PERCENT.get(stage, 0),
        profile=ProfileId.VIDEO_SHORT_V1,
        contract_version="v1",
        constraints=JobConstraints(max_duration_s=90, language="en", reading_level=None),
        failure=None,
        artifact_id=artifact_id,
        brief_id=None,
        version=version,
        created_at=created_at,
        updated_at=UPDATED_AT,
    )


def make_artifact(
    *,
    artifact_id: str = FIXED_ARTIFACT_ID,
    job_id: str = FIXED_JOB_ID,
    principal_id: str = DEFAULT_PRINCIPAL,
    role: ArtifactRole = ArtifactRole.PRIMARY,
    audience: Audience = Audience.LEARNER,
    created_at: datetime = CREATED_AT,
) -> ArtifactRecord:
    """An `artifacts` row as custody would hand one back."""
    return ArtifactRecord(
        artifact_id=artifact_id,
        job_id=job_id,
        principal_id=principal_id,
        chat_context_id=None,
        role=role,
        audience=audience,
        mime="video/mp4",
        rel_path=None,
        size_bytes=1024,
        content_hash=FIXED_CONTENT_HASH,
        storage_uri="s3://artifacts/lesson.mp4",
        probe={"duration_s": 87.5, "width": 1280, "height": 720},
        scan_verdict=ScanVerdict.CLEAN,
        validator_version="video.short.v1+checks.1",
        published_at=UPDATED_AT,
        created_at=created_at,
    )


async def _chunks() -> AsyncIterator[bytes]:
    for chunk in SAMPLE_BYTES:
        yield chunk


def make_stream(
    *,
    media_type: str = "video/mp4",
    filename: str = "lesson.mp4",
    disposition: str = "inline",
) -> ContentStream:
    return ContentStream(
        media_type=media_type,
        size_bytes=SAMPLE_SIZE,
        content_hash=FIXED_CONTENT_HASH,
        filename=filename,
        disposition="attachment" if disposition == "attachment" else "inline",
        chunks=_chunks(),
    )


# --------------------------------------------------------------------------- fakes


class FakePrincipalRepository:
    """In-memory `principals`, so the demo resolver can be exercised for real."""

    def __init__(self) -> None:
        self.rows: dict[str, Principal] = {}

    async def upsert(self, *, principal_id: PrincipalId, external_id: str) -> Principal:
        row = self.rows.get(principal_id)
        if row is None:
            row = Principal(
                principal_id=principal_id, external_id=external_id, created_at=CREATED_AT
            )
            self.rows[principal_id] = row
        return row


class FakeJobService:
    """Records what the edge handed it, and answers with whatever a test loaded."""

    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.submitted: list[tuple[AccessScope, SubmitJobCommand, Mapping[str, object]]] = []
        self.listed: list[tuple[AccessScope, Cursor | None, int]] = []
        self.submit_error: DomainError | None = None
        self.next_cursor: str | None = None

    def load(self, job: JobRecord) -> JobRecord:
        self.jobs[job.job_id] = job
        return job

    async def submit(
        self,
        scope: AccessScope,
        command: SubmitJobCommand,
        raw_body: Mapping[str, object],
    ) -> JobRecord:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append((scope, command, dict(raw_body)))
        request_key = new_request_key()
        job = replace(
            make_job(),
            job_id=derive_job_id(request_key),
            request_key=request_key,
            principal_id=scope.principal_id,
            chat_context_id=command.chat_context_id,
            profile=command.profile,
            constraints=command.constraints,
        )
        return self.load(job)

    async def get(self, scope: AccessScope, job_id: str) -> JobRecord:
        job = self.jobs.get(job_id)
        if job is None:
            raise DomainError(ErrorCode.JOB_NOT_FOUND)
        # @audit no ownership predicate. `scope` is accepted and never compared with
        # `job.principal_id`, which is exactly how the SQL repository behaves in the demo.
        return job

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        self.listed.append((scope, cursor, limit))
        newest_first = sorted(
            self.jobs.values(), key=lambda job: (job.created_at, job.job_id), reverse=True
        )
        return Page(items=tuple(newest_first[:limit]), next_cursor=self.next_cursor)


class FakeArtifactService:
    def __init__(self) -> None:
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.listed: list[tuple[str | None, Cursor | None, int]] = []
        self.content_error: DomainError | None = None
        self.stream: ContentStream | None = None
        self.next_cursor: str | None = None

    def load(self, artifact: ArtifactRecord) -> ArtifactRecord:
        self.artifacts[artifact.artifact_id] = artifact
        return artifact

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: str | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        self.listed.append((job_id, cursor, limit))
        rows = [row for row in self.artifacts.values() if job_id is None or row.job_id == job_id]
        rows.sort(key=lambda row: (row.created_at, row.artifact_id), reverse=True)
        return Page(items=tuple(rows[:limit]), next_cursor=self.next_cursor)

    async def open_content(self, scope: AccessScope, artifact_id: str) -> ContentStream:
        if self.content_error is not None:
            raise self.content_error
        if artifact_id not in self.artifacts:
            raise DomainError(ErrorCode.ARTIFACT_NOT_FOUND)
        return self.stream if self.stream is not None else make_stream()


class FakeDatabaseProbe:
    def __init__(self) -> None:
        self.reachable: bool = True
        self.raises: Exception | None = None

    async def ping(self) -> bool:
        if self.raises is not None:
            raise self.raises
        return self.reachable
