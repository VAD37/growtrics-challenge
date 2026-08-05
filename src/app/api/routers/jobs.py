"""`POST /v1/jobs`, `GET /v1/jobs/{job_id}`, `GET /v1/jobs`.

Steps 1, 2 and 3 of the success test in `docs/demo.md`. The router validates, maps, hands the
result to a service and shapes what comes back. It does no work and it touches no table: the
raw body travels across the seam so orchestration can write `requests` itself (scope override
item 4).

Submit always answers `202` with a fresh job. There is no idempotency key, so there is no
replay, no `409`, and no header to read (scope override item 2, supersedes D055 and D089).
Admission is one indexed count in the insert's transaction (D090), so it lives behind the seam
and all this module owns is the mapping from `TOO_MANY_ACTIVE_JOBS` to `429`.
"""

from collections.abc import Mapping
from typing import Annotated, Final

from fastapi import APIRouter, Path, Request, Response, status

from app.api.deps import ArtifactServiceDep, JobServiceDep, PageDep, ScopeDep
from app.api.errors import ERROR_RESPONSES
from app.api.ports import ArtifactService
from app.api.schemas.jobs import JobPage, JobView, etag_for, self_link
from app.api.schemas.requests import CreateJobRequest, to_command
from app.domain.access import AccessScope
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import JOB_ID_PATTERN
from app.domain.records import ArtifactRecord, JobRecord

router: Final[APIRouter] = APIRouter(prefix="/v1", tags=["jobs"], responses=ERROR_RESPONSES)

JobIdPath = Annotated[str, Path(pattern=JOB_ID_PATTERN)]
"""A malformed id is `400 INVALID_REQUEST`, not `404`. It never reaches a service, so "no such
job" would be a claim nobody checked, and answering it would make this endpoint an id-shape
oracle for free."""

ARTIFACTS_PER_JOB_SCAN: Final[int] = 24
"""How many artifact rows to read when filling in the primary on a job document.

`video.short.v1` allows 24 files (`plan/14-api-schema.md`), so one page covers a job's whole
output and the primary is either in it or not published yet.
"""


async def _primary_artifact(
    artifacts: ArtifactService, scope: AccessScope, job: JobRecord
) -> ArtifactRecord | None:
    """The primary artifact for a finished job, or `None`.

    One extra read, and only when `jobs.artifact_id` is set: a queued job costs nothing. A row
    that the job names and custody has not published yet leaves the field `null` rather than
    failing the read -- the client asked how the job is doing, and "done, bytes not there yet"
    is an answer it can act on.
    """
    if job.artifact_id is None:
        return None
    page = await artifacts.list(scope, job_id=job.job_id, cursor=None, limit=ARTIFACTS_PER_JOB_SCAN)
    for record in page.items:
        if record.artifact_id == job.artifact_id:
            return record
    return None


def _if_none_match_hits(header: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison, which is the only comparison `If-None-Match` uses.

    `W/"3"` and `"3"` are the same entity here, and a client may send a list. The tag itself is
    weak because the body is a rendering of the row rather than the bytes themselves.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = etag.removeprefix("W/")
    return any(candidate.strip().removeprefix("W/") == wanted for candidate in header.split(","))


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobView)
async def submit_job(
    payload: CreateJobRequest,
    request: Request,
    response: Response,
    scope: ScopeDep,
    jobs: JobServiceDep,
) -> JobView:
    """Accepted, queued, and never finished inside this call.

    `raw_body` is re-read from the request rather than dumped from `payload`, because the two
    are different documents on purpose: `requests` stores what the caller typed, whitespace and
    all, and the sealed brief stores what we asked for (scope override items 4 and 5).
    """
    parsed = await request.json()
    if not isinstance(parsed, dict):
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "body", "rule": "object"})
    raw_body: Mapping[str, object] = parsed
    job = await jobs.submit(scope, to_command(payload), raw_body)
    response.headers["Location"] = self_link(job.job_id)
    return JobView.from_record(job, artifact=None)


@router.get("/jobs/{job_id}", response_model=JobView)
async def get_job(
    job_id: JobIdPath,
    request: Request,
    response: Response,
    scope: ScopeDep,
    jobs: JobServiceDep,
    artifacts: ArtifactServiceDep,
) -> JobView | Response:
    """The polling endpoint, and therefore the one that has to stay cheap (D015, D084).

    A job that failed still answers `200` with `failure.code` populated: the request to read a
    failed job did not fail. A matching `If-None-Match` answers `304` before the artifact
    lookup, so an unchanged poll costs one read and no body.
    """
    job: JobRecord = await jobs.get(scope, job_id)
    etag = etag_for(job)
    if _if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return JobView.from_record(job, artifact=await _primary_artifact(artifacts, scope, job))


@router.get("/jobs", response_model=JobPage)
async def list_jobs(scope: ScopeDep, jobs: JobServiceDep, page: PageDep) -> JobPage:
    """Newest first, cursor paged, always an object (D072).

    Listed jobs carry `artifact: null` even when they have one. The seam reads artifacts one
    job at a time, so filling this in would be an extra query per row on the endpoint most
    likely to return a hundred of them. `links.artifacts` is on every item and the detail
    endpoint fills it in. Restoring it is a batch method on `ArtifactService`, not a new shape.
    """
    found = await jobs.list(scope, cursor=page.cursor, limit=page.limit)
    return JobPage(
        items=[JobView.from_record(job, artifact=None) for job in found.items],
        next_cursor=found.next_cursor,
    )
