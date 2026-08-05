"""The job document: exactly the keys `docs/demo.md` lists, and nothing the demo cut.

`JobView.from_record` is a pure function from a `jobs` row to the document a client reads, so
it is tested here without a client. The serialised key set is asserted literally: a field that
appears by accident is a field a client will depend on, and D091 removed `cost`, `links.events`
and `links.deliverable` on purpose.
"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Final

import pytest
from api_fakes import FIXED_TRACE_ID, make_artifact, make_job

from app.api.schemas.jobs import (
    JOB_DOCUMENT_KEYS,
    STAGE_STEP,
    ConstraintsView,
    JobView,
    artifacts_link,
    etag_for,
    self_link,
)
from app.domain.enums import JobStatus, ProfileId, StageName
from app.domain.errors import ERROR_CATALOG, ErrorCode
from app.domain.records import FailureRecord, JobConstraints, JobRecord

OCCURRED_AT: Final[datetime] = datetime(2026, 8, 5, 9, 13, 0, tzinfo=UTC)


def render(job: JobRecord) -> dict[str, object]:
    """Serialise the way FastAPI does: by alias, JSON types."""
    return JobView.from_record(job, artifact=None).model_dump(mode="json", by_alias=True)


# --------------------------------------------------------------------------- key set


def test_the_document_has_exactly_the_documented_keys() -> None:
    assert set(render(make_job())) == set(JOB_DOCUMENT_KEYS)


def test_the_cut_fields_are_absent() -> None:
    # D091: no cost block, no events or deliverable links. Each returns additively.
    body = render(make_job())
    assert "cost" not in body
    assert set(body["links"]) == {"self", "artifacts"}


def test_nulls_are_serialised_rather_than_omitted() -> None:
    # A client never branches on key presence.
    body = render(make_job())
    assert body["failure"] is None
    assert body["artifact"] is None
    assert body["chat_context_id"] is None


def test_the_request_key_is_on_the_document() -> None:
    # It joins this job's rows across the whole database; a caller debugging a demo needs it.
    job = make_job()
    assert render(job)["request_key"] == job.request_key


# --------------------------------------------------------------------------- fields


def test_scalar_fields_come_straight_off_the_row() -> None:
    job = make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING, version=4)
    body = render(job)
    assert body["job_id"] == job.job_id
    assert body["status"] == "RUNNING"
    assert body["stage"] == "GENERATING"
    assert body["attempt"] == 0
    assert body["profile"] == ProfileId.VIDEO_SHORT_V1.value
    assert body["contract_version"] == "v1"


def test_version_is_not_serialised() -> None:
    # `version` is the optimistic concurrency counter; the client sees it only as an ETag.
    assert "version" not in render(make_job(version=9))


def test_progress_percent_comes_from_the_row_not_from_the_stage_map() -> None:
    # D092 puts the map in the domain, but orchestration writes the column and it wins.
    job = replace(make_job(stage=StageName.GENERATING), progress_percent=17)
    assert render(job)["progress"]["percent"] == 17


def test_the_step_label_is_derived_from_the_stage() -> None:
    body = render(make_job(stage=StageName.GENERATING))
    assert body["progress"]["step"] == "generate"
    assert body["progress"]["message"]


@pytest.mark.parametrize("stage", list(StageName))
def test_every_stage_has_a_step_label(stage: StageName) -> None:
    # A missing entry would be a KeyError on a status poll, which is the one call that must work.
    assert stage in STAGE_STEP


def test_constraints_are_the_effective_ones_from_the_row() -> None:
    job = replace(
        make_job(),
        constraints=JobConstraints(max_duration_s=120, language="ms", reading_level=None),
    )
    assert render(job)["constraints"] == {
        "max_duration_s": 120,
        "language": "ms",
        "reading_level": None,
    }


def test_timestamps_are_rfc_3339_with_z() -> None:
    body = render(make_job())
    assert body["created_at"] == "2026-08-05T09:12:03Z"
    assert body["updated_at"].endswith("Z")


def test_links_point_at_this_job() -> None:
    job = make_job()
    body = render(job)
    assert body["links"]["self"] == f"/v1/jobs/{job.job_id}"
    assert body["links"]["artifacts"] == f"/v1/artifacts?job_id={job.job_id}"


def test_the_link_helpers_agree_with_the_document() -> None:
    job = make_job()
    body = render(job)
    assert body["links"]["self"] == self_link(job.job_id)
    assert body["links"]["artifacts"] == artifacts_link(job.job_id)


# --------------------------------------------------------------------------- failure


def failed_job() -> JobRecord:
    failure = FailureRecord(
        code=ErrorCode.GENERATION_FAILED,
        stage=StageName.GENERATING,
        message=ERROR_CATALOG[ErrorCode.GENERATION_FAILED].message,
        retryable=False,
        occurred_at=OCCURRED_AT,
        trace_id=FIXED_TRACE_ID,
    )
    return replace(make_job(status=JobStatus.FAILED, stage=StageName.FAILED), failure=failure)


def test_a_failed_job_carries_the_failure_block() -> None:
    body = render(failed_job())
    assert body["status"] == "FAILED"
    assert body["failure"] == {
        "code": "GENERATION_FAILED",
        "stage": "GENERATING",
        "message": ERROR_CATALOG[ErrorCode.GENERATION_FAILED].message,
        "retryable": False,
        "occurred_at": "2026-08-05T09:13:00Z",
        "trace_id": FIXED_TRACE_ID,
    }


def test_the_failure_message_comes_from_the_catalog() -> None:
    # D062: no message is written at a raise site, and none is written here either.
    body = render(failed_job())
    assert body["failure"]["message"] == ERROR_CATALOG[ErrorCode.GENERATION_FAILED].message


# --------------------------------------------------------------------------- artifact


def test_a_succeeded_job_embeds_the_primary_artifact() -> None:
    artifact = make_artifact()
    job = make_job(
        status=JobStatus.SUCCEEDED, stage=StageName.DONE, artifact_id=artifact.artifact_id
    )
    body = JobView.from_record(job, artifact=artifact).model_dump(mode="json", by_alias=True)
    assert body["artifact"]["artifact_id"] == artifact.artifact_id
    assert body["artifact"]["content_url"] == f"/v1/artifacts/{artifact.artifact_id}/content"


def test_the_embedded_artifact_does_not_repeat_the_schema_version() -> None:
    artifact = make_artifact()
    job = make_job(
        status=JobStatus.SUCCEEDED, stage=StageName.DONE, artifact_id=artifact.artifact_id
    )
    body = JobView.from_record(job, artifact=artifact).model_dump(mode="json", by_alias=True)
    assert "schema_version" not in body["artifact"]


# --------------------------------------------------------------------------- etag


def test_the_etag_is_weak_and_keyed_to_the_version() -> None:
    # D084, narrowed: the demo has no job_events, so there is no last_seq half to include.
    assert etag_for(make_job(version=7)) == 'W/"7"'


def test_the_etag_moves_when_the_version_does() -> None:
    assert etag_for(make_job(version=1)) != etag_for(make_job(version=2))


# --------------------------------------------------------------------------- view shapes


def test_response_views_are_frozen() -> None:
    view = ConstraintsView(max_duration_s=90, language="en", reading_level=None)
    with pytest.raises(ValueError, match="frozen"):
        view.max_duration_s = 30
