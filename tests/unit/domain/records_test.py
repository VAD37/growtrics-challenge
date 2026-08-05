"""Domain records: the vocabulary every lane imports.

Field-name assertions look pedantic and are the point. These records are the in-process shape
of frozen SQL columns, and a field quietly renamed on one side of that seam is a column that
stops being written with no test failing anywhere else.
"""

from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from typing import Final

import pytest

from app.domain.enums import (
    ArtifactRole,
    Audience,
    ContextKind,
    JobStatus,
    ProfileId,
    ReadingLevel,
    ScanVerdict,
    StageName,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.records import (
    ArtifactRecord,
    BriefRecord,
    ClaimedWorkItem,
    ContentStream,
    ContextItem,
    Cursor,
    FailureRecord,
    JobConstraints,
    JobRecord,
    Page,
    StoredRequest,
    SubmitJobCommand,
)

AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
JOB_ID: Final[str] = "job_" + "0" * 26
ARTIFACT_ID: Final[str] = "art_" + "0" * 26
BRIEF_ID: Final[str] = "brf_" + "0" * 26
WORK_ITEM_ID: Final[str] = "wi_" + "0" * 26
TRACE_ID: Final[str] = "tr_" + "0" * 26
REQUEST_KEY: Final[str] = "req_" + "0" * 26
PRINCIPAL_ID: Final[str] = "u_demo"


def _constraints() -> JobConstraints:
    return JobConstraints(max_duration_s=90, language="en", reading_level=None)


def _job() -> JobRecord:
    return JobRecord(
        job_id=JOB_ID,
        request_key=REQUEST_KEY,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        status=JobStatus.QUEUED,
        stage=StageName.INTAKE,
        attempt=0,
        progress_percent=10,
        profile=ProfileId.VIDEO_SHORT_V1,
        contract_version="v1",
        constraints=_constraints(),
        failure=None,
        artifact_id=None,
        brief_id=None,
        version=0,
        created_at=AT,
        updated_at=AT,
    )


def _artifact() -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=ARTIFACT_ID,
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        role=ArtifactRole.PRIMARY,
        audience=Audience.LEARNER,
        mime="video/mp4",
        rel_path=None,
        size_bytes=1024,
        content_hash="sha256:" + "ab" * 32,
        storage_uri="s3://artifacts/job_x/primary.mp4",
        probe={"duration_s": 42.0},
        scan_verdict=ScanVerdict.CLEAN,
        validator_version="v1",
        published_at=AT,
        created_at=AT,
    )


# --------------------------------------------------------------------------- shape


ALL_RECORDS: Final[tuple[type, ...]] = (
    ContextItem,
    JobConstraints,
    SubmitJobCommand,
    StoredRequest,
    FailureRecord,
    BriefRecord,
    JobRecord,
    ArtifactRecord,
    ContentStream,
    ClaimedWorkItem,
    Cursor,
    Page,
)


@pytest.mark.parametrize("record", ALL_RECORDS)
def test_every_record_is_a_frozen_slotted_dataclass(record: type) -> None:
    # No pydantic in the domain: these are pure types, and slots keeps a typo an AttributeError.
    assert is_dataclass(record)
    assert record.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert "__slots__" in record.__dict__


def test_records_reject_mutation() -> None:
    job = _job()
    with pytest.raises(FrozenInstanceError):
        job.status = JobStatus.RUNNING  # type: ignore[misc]


def _names(record: type) -> set[str]:
    return {field.name for field in fields(record)}


def test_job_record_matches_the_jobs_row() -> None:
    assert _names(JobRecord) == {
        "job_id",
        "request_key",
        "principal_id",
        "chat_context_id",
        "status",
        "stage",
        "attempt",
        "progress_percent",
        "profile",
        "contract_version",
        "constraints",
        "failure",
        "artifact_id",
        "brief_id",
        "version",
        "created_at",
        "updated_at",
    }


def test_artifact_record_matches_the_artifacts_row() -> None:
    assert _names(ArtifactRecord) == {
        "artifact_id",
        "job_id",
        "principal_id",
        "chat_context_id",
        "role",
        "audience",
        "mime",
        "rel_path",
        "size_bytes",
        "content_hash",
        "storage_uri",
        "probe",
        "scan_verdict",
        "validator_version",
        "published_at",
        "created_at",
    }


def test_stored_request_matches_the_requests_row() -> None:
    assert _names(StoredRequest) == {"request_key", "principal_id", "raw", "received_at"}


def test_brief_record_matches_the_briefs_row() -> None:
    assert _names(BriefRecord) == {
        "brief_id",
        "job_id",
        "brief_hash",
        "template_version",
        "subject",
        "concept_id",
        "instruction",
        "context_items",
        "constraints",
        "guard_verdict",
        "sealed_at",
    }


def test_job_record_carries_no_idempotency_field() -> None:
    # Scope override: the header, the table, and the column are all gone.
    assert not any("idempotenc" in name or "digest" in name for name in _names(JobRecord))


def test_stored_request_keeps_the_body_as_received() -> None:
    raw: dict[str, object] = {"instruction": "why do atoms bond", "context": []}
    stored = StoredRequest(
        request_key=REQUEST_KEY, principal_id=PRINCIPAL_ID, raw=raw, received_at=AT
    )
    assert stored.raw == raw


def test_submit_command_holds_typed_context() -> None:
    command = SubmitJobCommand(
        instruction="why do atoms form covalent bonds",
        context=(ContextItem(kind=ContextKind.LEVEL, text="grade 9"),),
        constraints=JobConstraints(
            max_duration_s=90, language="en", reading_level=ReadingLevel.LOWER_SECONDARY
        ),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )
    assert command.context[0].kind is ContextKind.LEVEL


def test_failure_record_carries_a_code_and_not_a_free_message_source() -> None:
    failure = FailureRecord(
        code=ErrorCode.GENERATION_FAILED,
        stage=StageName.GENERATING,
        message="The lesson could not be generated.",
        retryable=False,
        occurred_at=AT,
        trace_id=TRACE_ID,
    )
    assert failure.code is ErrorCode.GENERATION_FAILED


def test_claimed_work_item_carries_what_a_lease_needs() -> None:
    assert _names(ClaimedWorkItem) == {
        "item_id",
        "job_id",
        "claimed_by",
        "claimed_until",
        "claim_count",
    }
    item = ClaimedWorkItem(
        item_id=WORK_ITEM_ID,
        job_id=JOB_ID,
        claimed_by="worker-1",
        claimed_until=AT,
        claim_count=1,
    )
    assert item.claim_count == 1


# --------------------------------------------------------------------------- content stream


async def test_content_stream_yields_its_bytes() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"abc"
        yield b"def"

    stream = ContentStream(
        media_type="video/mp4",
        size_bytes=6,
        content_hash="sha256:" + "ab" * 32,
        filename="lesson.mp4",
        disposition="inline",
        chunks=chunks(),
    )
    assert b"".join([chunk async for chunk in stream.chunks]) == b"abcdef"
    assert stream.disposition == "inline"


# --------------------------------------------------------------------------- cursor


def test_cursor_round_trips() -> None:
    cursor = Cursor(created_at=AT, id=JOB_ID)
    assert Cursor.decode(cursor.encode()) == cursor


def test_cursor_is_opaque_and_url_safe() -> None:
    # D072: an opaque keyset cursor, never an offset. Nothing readable, nothing to increment.
    token = Cursor(created_at=AT, id=JOB_ID).encode()
    assert JOB_ID not in token
    assert "2026" not in token
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_cursor_is_not_an_offset() -> None:
    assert not Cursor(created_at=AT, id=JOB_ID).encode().isdigit()


def test_two_cursors_differ_when_either_half_differs() -> None:
    other_time = datetime(2026, 8, 5, 9, 12, 4, tzinfo=UTC)
    base = Cursor(created_at=AT, id=JOB_ID).encode()
    assert Cursor(created_at=other_time, id=JOB_ID).encode() != base
    assert Cursor(created_at=AT, id="job_" + "1" * 26).encode() != base


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!!",
        "not-base64-@@",
        "YWJj",  # decodes, but carries no separator
        "MjAyNi0wOC0wNQ==",  # a date and nothing else
        "a" * 4096,
    ],
)
def test_malformed_cursor_is_a_domain_error_not_a_crash(token: str) -> None:
    with pytest.raises(DomainError) as caught:
        Cursor.decode(token)
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_cursor_rejects_a_naive_timestamp() -> None:
    # Every timestamp in this system is timestamptz; a naive one is a bug, not a default.
    with pytest.raises(DomainError) as caught:
        Cursor(created_at=datetime(2026, 8, 5, 9, 12, 3), id=JOB_ID).encode()
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_cursor_decode_rejects_a_naive_timestamp() -> None:
    smuggled = Cursor._encode_parts("2026-08-05T09:12:03", JOB_ID)
    with pytest.raises(DomainError) as caught:
        Cursor.decode(smuggled)
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_cursor_decode_rejects_an_empty_id() -> None:
    smuggled = Cursor._encode_parts(AT.isoformat(), "")
    with pytest.raises(DomainError) as caught:
        Cursor.decode(smuggled)
    assert caught.value.code is ErrorCode.INVALID_REQUEST


# --------------------------------------------------------------------------- page


def test_page_carries_items_and_a_next_cursor() -> None:
    page: Page[JobRecord] = Page(items=(_job(),), next_cursor=None)
    assert len(page.items) == 1
    assert page.next_cursor is None


def test_page_is_generic_over_its_item_type() -> None:
    artifacts: Page[ArtifactRecord] = Page(items=(_artifact(),), next_cursor="opaque")
    assert artifacts.items[0].role is ArtifactRole.PRIMARY
    assert artifacts.next_cursor == "opaque"


def test_empty_page_is_representable() -> None:
    page: Page[JobRecord] = Page(items=(), next_cursor=None)
    assert page.items == ()
