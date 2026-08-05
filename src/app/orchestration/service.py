"""The five use cases the API reaches through, and the two facades that shape them for it.

`app.api.ports` declares `JobService` and `ArtifactService` as `Protocol`s. `JobService` and
`ArtifactService` below satisfy them structurally, which is the whole reason they are protocols:
orchestration never imports `app.api`, and the import-linter contract keeps it that way.

Each use case is its own class so that "submit a job" is a thing with a name, a constructor and
a set of collaborators rather than a method on a god object. The facades exist because the API
wants two objects, not five.

What is deliberately absent, per the scope override:

- No idempotency lookup, no replay branch, no `409` (item 2). Two identical submits mint two
  request keys and become two jobs.
- No ownership predicate on any read (item 1). Every read still takes an `AccessScope` first
  (D067), and every read site that ignores it says so with an `@audit`.
"""

from collections.abc import Callable, Mapping
from typing import Final

from app.config import settings
from app.domain.access import AccessScope
from app.domain.enums import JobStatus, StageName, percent_for
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import (
    ArtifactId,
    JobId,
    RequestKey,
    derive_job_id,
    derive_work_item_id,
    new_request_key,
)
from app.domain.records import (
    ArtifactRecord,
    ContentStream,
    Cursor,
    JobRecord,
    Page,
    StoredRequest,
    SubmitJobCommand,
)
from app.orchestration.ports import (
    AdmissionPolicy,
    ArtifactReader,
    Clock,
    JobRepository,
    QueuedWorkItem,
    Submission,
    UnitOfWork,
)

DEFAULT_CONTRACT_VERSION: Final[str] = "v1"
"""The `jobs.output_contract` column: under which rules, not what to make (A4).

`profile` answers what to make and comes from the request. This one is server-owned and there is
one value of it, because there is one version of the contract.
"""

INITIAL_STAGE: Final[StageName] = StageName.INTAKE
"""Where a job sits between being accepted and being claimed. Worth 10 percent (D092)."""


def _clamp_limit(limit: int, max_limit: int) -> int:
    """A page size is a client-supplied integer, so it is bounded rather than trusted.

    Over the ceiling is clamped, because asking for more than we serve is not a mistake worth a
    `400`. Zero or negative is rejected, because it is not a page size at all.
    """
    if limit < 1:
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "limit", "expected": "at least 1"})
    return min(limit, max_limit)


# --------------------------------------------------------------------------- admission


class ActiveJobLimit:
    """The capacity gate in front of submit: three in flight is the ceiling (D090).

    One indexed count against `jobs_active`, run before anything is inserted. It is not a rate
    limit and it is not an authorisation check; it is the answer to "can this service take
    another job right now", and it is the only abuse control left after cost metering was cut.
    """

    def __init__(
        self, jobs: JobRepository, *, max_active: int = settings.admission_max_active_jobs
    ) -> None:
        self._jobs: JobRepository = jobs
        self._max_active: int = max_active

    async def admit(self, scope: AccessScope) -> None:
        active = await self._jobs.count_active(scope, scope.principal_id)
        if active >= self._max_active:
            raise DomainError(ErrorCode.TOO_MANY_ACTIVE_JOBS, {"limit": str(self._max_active)})


# --------------------------------------------------------------------------- use cases


class SubmitJob:
    """`POST /v1/jobs`: gate, mint, derive, write once.

    The order matters. Admission runs first so a refused caller costs one indexed count and no
    insert. The request key is minted next and every id in the request's story is derived from
    it, so all three rows can be built in memory before a transaction opens. Then one write.
    """

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Clock,
        admission: AdmissionPolicy,
        mint_request_key: Callable[[], RequestKey] = new_request_key,
    ) -> None:
        self._uow: UnitOfWork = uow
        self._clock: Clock = clock
        self._admission: AdmissionPolicy = admission
        self._mint: Callable[[], RequestKey] = mint_request_key

    async def execute(
        self, scope: AccessScope, command: SubmitJobCommand, raw_body: Mapping[str, object]
    ) -> JobRecord:
        await self._admission.admit(scope)

        request_key = self._mint()
        job_id = derive_job_id(request_key)
        now = self._clock.now()

        stored = StoredRequest(
            request_key=request_key,
            principal_id=scope.principal_id,
            raw=raw_body,
            received_at=now,
        )
        job = JobRecord(
            job_id=job_id,
            request_key=request_key,
            principal_id=scope.principal_id,
            chat_context_id=command.chat_context_id,
            status=JobStatus.QUEUED,
            stage=INITIAL_STAGE,
            attempt=0,
            progress_percent=percent_for(INITIAL_STAGE),
            profile=command.profile,
            contract_version=DEFAULT_CONTRACT_VERSION,
            constraints=command.constraints,
            failure=None,
            artifact_id=None,
            brief_id=None,
            version=0,
            created_at=now,
            updated_at=now,
        )
        work_item = QueuedWorkItem(
            item_id=derive_work_item_id(job_id),
            job_id=job_id,
            available_at=now,
            created_at=now,
        )

        await self._uow.commit_submission(Submission(request=stored, job=job, work_item=work_item))
        return job


class QueryJob:
    """`GET /v1/jobs/{job_id}`."""

    def __init__(self, jobs: JobRepository) -> None:
        self._jobs: JobRepository = jobs

    async def execute(self, scope: AccessScope, job_id: JobId) -> JobRecord:
        # @audit no ownership check. The scope is passed and not consulted: any caller reads any
        # job (scope override item 1, supersedes D067's enforcement half). `docs/demo.md` says a
        # second user id gets 404 here; it does not, and that is the deliberate hole.
        job = await self._jobs.get(scope, job_id)
        if job is None:
            raise DomainError(ErrorCode.JOB_NOT_FOUND)
        return job


class ListJobs:
    """`GET /v1/jobs`, newest first."""

    def __init__(
        self,
        jobs: JobRepository,
        *,
        max_limit: int = settings.page_max_limit,
    ) -> None:
        self._jobs: JobRepository = jobs
        self._max_limit: int = max_limit

    async def execute(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        # @audit no ownership check. This lists every job in the database, not the caller's.
        return await self._jobs.list(
            scope, cursor=cursor, limit=_clamp_limit(limit, self._max_limit)
        )


class ListArtifacts:
    """`GET /v1/artifacts`, with the optional `?job_id=` filter that makes step 4 one endpoint."""

    def __init__(
        self,
        artifacts: ArtifactReader,
        *,
        max_limit: int = settings.page_max_limit,
    ) -> None:
        self._artifacts: ArtifactReader = artifacts
        self._max_limit: int = max_limit

    async def execute(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        # @audit no ownership check. Passing a stranger's job id lists their artifacts.
        return await self._artifacts.list(
            scope, job_id=job_id, cursor=cursor, limit=_clamp_limit(limit, self._max_limit)
        )


class OpenArtifactContent:
    """`GET /v1/artifacts/{artifact_id}/content`: the bytes, streamed."""

    def __init__(self, artifacts: ArtifactReader) -> None:
        self._artifacts: ArtifactReader = artifacts

    async def execute(self, scope: AccessScope, artifact_id: ArtifactId) -> ContentStream:
        # @audit no ownership check. Any caller streams any artifact's bytes.
        stream = await self._artifacts.open_content(scope, artifact_id)
        if stream is None:
            raise DomainError(ErrorCode.ARTIFACT_NOT_FOUND)
        return stream


# --------------------------------------------------------------------------- the api seam


class JobService:
    """Satisfies `app.api.ports.JobService` by shape. This module imports no part of the API."""

    def __init__(self, *, submit: SubmitJob, query: QueryJob, listing: ListJobs) -> None:
        self._submit: SubmitJob = submit
        self._query: QueryJob = query
        self._listing: ListJobs = listing

    async def submit(
        self, scope: AccessScope, command: SubmitJobCommand, raw_body: Mapping[str, object]
    ) -> JobRecord:
        return await self._submit.execute(scope, command, raw_body)

    async def get(self, scope: AccessScope, job_id: JobId) -> JobRecord:
        return await self._query.execute(scope, job_id)

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        return await self._listing.execute(scope, cursor=cursor, limit=limit)


class ArtifactService:
    """Satisfies `app.api.ports.ArtifactService` by shape."""

    def __init__(self, *, listing: ListArtifacts, content: OpenArtifactContent) -> None:
        self._listing: ListArtifacts = listing
        self._content: OpenArtifactContent = content

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        return await self._listing.execute(scope, job_id=job_id, cursor=cursor, limit=limit)

    async def open_content(self, scope: AccessScope, artifact_id: ArtifactId) -> ContentStream:
        return await self._content.execute(scope, artifact_id)
