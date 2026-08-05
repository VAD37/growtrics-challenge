"""The job document. One shape for `POST /v1/jobs`, `GET /v1/jobs/{id}` and each list item.

`docs/demo.md` quotes it field for field. Against `plan/14-api-schema.md` it loses `cost`,
`links.events` and `links.deliverable` (D091), and it gains `request_key`, which is the linking
key one submission is known by across every table (scope override item 3) and the first thing
worth having when a demo goes sideways.

Nulls are serialised rather than omitted, so a client never branches on key presence. The
serialised key set is written down as `JOB_DOCUMENT_KEYS` because "what does this endpoint
return" should be a list somebody can read, not the result of walking a class hierarchy.
"""

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import Field

from app.api.schemas.artifacts import ArtifactSummaryView
from app.api.schemas.common import NestedView, PageView, ResponseDocument
from app.domain.enums import JobStatus, ProfileId, ReadingLevel, StageName
from app.domain.errors import ErrorCode
from app.domain.ids import ChatContextId, JobId, RequestKey, TraceId
from app.domain.records import ArtifactRecord, JobRecord

SELF_PATH_TEMPLATE: Final[str] = "/v1/jobs/{job_id}"
ARTIFACTS_PATH_TEMPLATE: Final[str] = "/v1/artifacts?job_id={job_id}"

JOB_DOCUMENT_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "job_id",
    "request_key",
    "chat_context_id",
    "status",
    "stage",
    "attempt",
    "progress",
    "profile",
    "contract_version",
    "constraints",
    "failure",
    "artifact",
    "created_at",
    "updated_at",
    "links",
)

STAGE_STEP: Final[Mapping[StageName, tuple[str, str]]] = MappingProxyType(
    {
        StageName.INTAKE: ("intake", "reading the request"),
        StageName.ADMISSION: ("admit", "checking capacity"),
        StageName.PLACEMENT: ("place", "finding a worker"),
        StageName.PREPARING: ("prepare", "writing the brief"),
        StageName.GENERATING: ("generate", "rendering"),
        StageName.COLLECTING: ("collect", "harvesting output"),
        StageName.VERIFYING: ("verify", "checking output"),
        StageName.PUBLISHING: ("publish", "storing the lesson"),
        StageName.DONE: ("done", "finished"),
        StageName.FAILED: ("failed", "stopped"),
    }
)
"""Stage to `(step, message)`, total over `StageName` and asserted so in the tests.

Presentation, not a rule: the percent comes off the row (D092, written by orchestration) and
these two strings are what a progress bar puts next to it. `ADMISSION` and `PLACEMENT` belong
to stages the demo does not build and are here because a `KeyError` on the one endpoint a
client polls is not an acceptable way to find that out.
"""


def self_link(job_id: JobId) -> str:
    return SELF_PATH_TEMPLATE.format(job_id=job_id)


def artifacts_link(job_id: JobId) -> str:
    return ARTIFACTS_PATH_TEMPLATE.format(job_id=job_id)


def etag_for(job: JobRecord) -> str:
    """`W/"{version}"` (D084, narrowed by the demo cut).

    The frozen design keys the tag to `version` **and** the last event `seq`, because a job
    document that carries events changes when either moves. The demo has no `job_events` table
    (D093), so there is no second half to include and adding a constant one would be a lie
    about what the tag covers. Weak, because the body is a rendering of the row rather than
    the bytes themselves.
    """
    return f'W/"{job.version}"'


class ProgressView(NestedView):
    percent: Annotated[int, Field(ge=0, le=100)]
    step: str
    message: str


class ConstraintsView(NestedView):
    """Effective values after profile caps, not an echo of the request (D080)."""

    max_duration_s: int
    language: str
    reading_level: ReadingLevel | None


class FailureView(NestedView):
    """Why a job stopped. Present on a `200`: the request to read a failed job did not fail."""

    code: ErrorCode
    stage: StageName
    message: str
    retryable: bool
    occurred_at: datetime
    trace_id: TraceId


class JobLinks(NestedView):
    """`self` and `artifacts`. `events` and `deliverable` return with their stages (D091).

    The field is `self_` with an alias because `self` is the receiver of every Python method;
    FastAPI serialises response models by alias, so the wire name is `self` as documented.
    """

    self_: Annotated[str, Field(alias="self")]
    artifacts: str


class JobView(ResponseDocument):
    """The whole answer to "what is my job doing", per `docs/demo.md`."""

    job_id: JobId
    request_key: RequestKey
    chat_context_id: ChatContextId | None
    status: JobStatus
    stage: StageName
    attempt: int
    progress: ProgressView
    profile: ProfileId
    contract_version: str
    constraints: ConstraintsView
    failure: FailureView | None
    artifact: ArtifactSummaryView | None
    created_at: datetime
    updated_at: datetime
    links: JobLinks

    @classmethod
    def from_record(cls, job: JobRecord, artifact: ArtifactRecord | None) -> JobView:
        """Pure mapping from the `jobs` row to the document.

        `artifact` is the **primary** only, and it is passed in rather than fetched: this
        module does no I/O, and the router decides whether one lookup is worth it (see
        `app/api/routers/jobs.py`). `version` is not serialised at all -- a client sees it only
        as an `ETag`, which is what it is for.
        """
        step, message = STAGE_STEP[job.stage]
        summary = None if artifact is None else ArtifactSummaryView.from_record(artifact)
        return cls(
            job_id=job.job_id,
            request_key=job.request_key,
            chat_context_id=job.chat_context_id,
            status=job.status,
            stage=job.stage,
            attempt=job.attempt,
            progress=ProgressView(percent=job.progress_percent, step=step, message=message),
            profile=job.profile,
            contract_version=job.contract_version,
            constraints=ConstraintsView(
                max_duration_s=job.constraints.max_duration_s,
                language=job.constraints.language,
                reading_level=job.constraints.reading_level,
            ),
            failure=None
            if job.failure is None
            else FailureView(
                code=job.failure.code,
                stage=job.failure.stage,
                message=job.failure.message,
                retryable=job.failure.retryable,
                occurred_at=job.failure.occurred_at,
                trace_id=job.failure.trace_id,
            ),
            artifact=summary,
            created_at=job.created_at,
            updated_at=job.updated_at,
            links=JobLinks.model_validate(
                {"self": self_link(job.job_id), "artifacts": artifacts_link(job.job_id)}
            ),
        )


class JobPage(PageView[JobView]):
    """`GET /v1/jobs`. Newest first, cursor paged."""
