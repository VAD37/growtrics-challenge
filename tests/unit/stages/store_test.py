"""Publishing: bytes to the object store, one row to `artifacts`, and nothing else.

Custody is the sole writer of that table and of those bytes (D066), and it never writes job
status. The order assertion below is the one that matters operationally: bytes first, row
second, so the reachable state is always "row implies bytes".
"""

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Final

import pytest

from app.custody.harvester import content_hash_of
from app.custody.ports import ArtifactStore, ArtifactWriter
from app.custody.store import (
    ArtifactPublisher,
    artifact_record_for,
    content_stream,
    object_key,
)
from app.custody.verifier import ContractResultValidator
from app.domain.artifact import ArtifactDescriptor, HarvestedFile, VerifiedArtifact
from app.domain.contracts import contract_for
from app.domain.enums import ArtifactRole, Audience, ProfileId, ScanVerdict
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import derive_artifact_id
from app.domain.records import ArtifactRecord
from app.generation.backends.mock import SAMPLE_VIDEO_PATH

CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)
JOB_ID: Final[str] = "job_" + "0" * 26
PRINCIPAL_ID: Final[str] = "u_demo"
NOW: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
REAL_MP4: Final[bytes] = SAMPLE_VIDEO_PATH.read_bytes()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.log: list[str] = []

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        self.log.append(f"put:{key}")
        self.objects[key] = data
        return f"s3://artifacts/{key}"

    async def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        raise NotImplementedError(storage_uri)


class FakeWriter:
    def __init__(self, store: FakeStore) -> None:
        self.rows: list[ArtifactRecord] = []
        self._store: FakeStore = store

    async def insert(self, record: ArtifactRecord) -> ArtifactRecord:
        self._store.log.append(f"insert:{record.artifact_id}")
        self.rows.append(record)
        return record


def _verified(
    data: bytes = REAL_MP4,
    *,
    role: ArtifactRole = ArtifactRole.PRIMARY,
    media_type: str = "video/mp4",
    rel_path: str = "out/lesson.mp4",
    audience: Audience = Audience.LEARNER,
) -> VerifiedArtifact:
    harvested = HarvestedFile(
        descriptor=ArtifactDescriptor(
            role=role,
            audience=audience,
            media_type=media_type,
            rel_path=rel_path,
            declared_size_bytes=len(data),
            declared_sha256=None,
        ),
        data=data,
        size_bytes=len(data),
        content_hash=content_hash_of(data),
        claim_agreed=True,
    )
    return ContractResultValidator().verify(harvested, CONTRACT, requested_max_duration_s=90)


# --------------------------------------------------------------------------- keys


def test_object_key_is_content_addressed_under_the_job() -> None:
    key = object_key(JOB_ID, content_hash_of(REAL_MP4), "video/mp4", ScanVerdict.CLEAN)
    digest = content_hash_of(REAL_MP4).removeprefix("sha256:")
    assert key == f"artifacts/{JOB_ID}/{digest}.mp4"


def test_a_quarantined_object_lands_somewhere_else() -> None:
    """No public route to a failing candidate; an operator can still find it."""
    key = object_key(JOB_ID, content_hash_of(b"x"), "video/mp4", ScanVerdict.QUARANTINED)
    assert key.startswith("quarantine/")


@pytest.mark.parametrize(
    ("media_type", "suffix"),
    [
        ("video/mp4", ".mp4"),
        ("image/png", ".png"),
        ("image/jpeg", ".jpg"),
        ("text/plain", ".txt"),
        ("text/vtt", ".vtt"),
        ("application/x-ndjson", ".jsonl"),
        ("application/octet-stream", ".bin"),
    ],
)
def test_the_suffix_follows_the_media_type(media_type: str, suffix: str) -> None:
    key = object_key(JOB_ID, content_hash_of(b"x"), media_type, ScanVerdict.CLEAN)
    assert key.endswith(suffix)


# --------------------------------------------------------------------------- the row


def test_the_row_copies_the_principal_at_insert() -> None:
    """A6, D088. Listing a learner's artifacts stays one index scan with no chat context."""
    record = artifact_record_for(
        _verified(),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        storage_uri="s3://artifacts/x",
        now=NOW,
    )
    assert record.principal_id == PRINCIPAL_ID
    assert record.chat_context_id is None
    assert record.job_id == JOB_ID


def test_the_row_derives_its_id_from_the_job_and_the_hash() -> None:
    record = artifact_record_for(
        _verified(),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        storage_uri="s3://artifacts/x",
        now=NOW,
    )
    assert record.artifact_id == derive_artifact_id(JOB_ID, content_hash_of(REAL_MP4))


def test_the_row_carries_the_three_questions_the_old_kind_folded_together() -> None:
    record = artifact_record_for(
        _verified(),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id="ctx_demo",
        storage_uri="s3://artifacts/x",
        now=NOW,
    )
    assert record.role is ArtifactRole.PRIMARY
    assert record.audience is Audience.LEARNER
    assert record.mime == "video/mp4"
    assert record.scan_verdict is ScanVerdict.CLEAN
    assert record.validator_version == ContractResultValidator.version
    assert record.rel_path == "out/lesson.mp4"
    assert record.size_bytes == len(REAL_MP4)
    assert record.content_hash == content_hash_of(REAL_MP4)
    assert record.chat_context_id == "ctx_demo"


def test_a_learner_artifact_is_published_and_an_operator_one_is_not() -> None:
    learner = artifact_record_for(
        _verified(),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        storage_uri="s3://a",
        now=NOW,
    )
    operator = artifact_record_for(
        _verified(
            b'{"event":"start"}\n',
            role=ArtifactRole.LOG,
            media_type="application/x-ndjson",
            rel_path="out/agent.jsonl",
            audience=Audience.OPERATOR,
        ),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        storage_uri="s3://b",
        now=NOW,
    )
    assert learner.published_at == NOW
    assert operator.published_at is None


def test_a_quarantined_artifact_is_never_published() -> None:
    record = artifact_record_for(
        _verified(b"\x00\x00"),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        storage_uri="s3://a",
        now=NOW,
    )
    assert record.scan_verdict is ScanVerdict.QUARANTINED
    assert record.published_at is None


# --------------------------------------------------------------------------- publishing


async def test_publish_writes_bytes_before_the_row() -> None:
    store = FakeStore()
    writer = FakeWriter(store)
    record = await ArtifactPublisher(store, writer).publish(
        _verified(),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        now=NOW,
    )
    assert store.log == [
        f"put:{object_key(JOB_ID, record.content_hash, 'video/mp4', ScanVerdict.CLEAN)}",
        f"insert:{record.artifact_id}",
    ]
    assert store.objects[next(iter(store.objects))] == REAL_MP4
    assert writer.rows == [record]
    assert record.storage_uri.startswith("s3://")


async def test_publish_all_returns_one_row_per_verified_file() -> None:
    store = FakeStore()
    writer = FakeWriter(store)
    records = await ArtifactPublisher(store, writer).publish_all(
        (
            _verified(),
            _verified(
                b'{"event":"start"}\n',
                role=ArtifactRole.LOG,
                media_type="application/x-ndjson",
                rel_path="out/agent.jsonl",
                audience=Audience.OPERATOR,
            ),
        ),
        job_id=JOB_ID,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        now=NOW,
    )
    assert len(records) == 2
    assert {record.role for record in records} == {ArtifactRole.PRIMARY, ArtifactRole.LOG}
    assert len(store.objects) == 2


def test_the_publisher_satisfies_both_ports_it_is_handed() -> None:
    store = FakeStore()
    assert isinstance(store, ArtifactStore)
    assert isinstance(FakeWriter(store), ArtifactWriter)


# --------------------------------------------------------------------------- serving


async def _chunks() -> AsyncIterator[bytes]:
    yield REAL_MP4


def _record(**overrides: object) -> ArtifactRecord:
    base: Mapping[str, object] = {
        "artifact_id": derive_artifact_id(JOB_ID, content_hash_of(REAL_MP4)),
        "job_id": JOB_ID,
        "principal_id": PRINCIPAL_ID,
        "chat_context_id": None,
        "role": ArtifactRole.PRIMARY,
        "audience": Audience.LEARNER,
        "mime": "video/mp4",
        "rel_path": "out/lesson.mp4",
        "size_bytes": len(REAL_MP4),
        "content_hash": content_hash_of(REAL_MP4),
        "storage_uri": "s3://artifacts/x",
        "probe": {},
        "scan_verdict": ScanVerdict.CLEAN,
        "validator_version": ContractResultValidator.version,
        "published_at": NOW,
        "created_at": NOW,
    }
    return ArtifactRecord(**{**base, **overrides})  # type: ignore[arg-type]


def test_content_stream_takes_its_disposition_from_the_contract() -> None:
    stream = content_stream(_record(), _chunks(), CONTRACT)
    assert stream.disposition == CONTRACT.serving.disposition
    assert stream.media_type == "video/mp4"
    assert stream.size_bytes == len(REAL_MP4)
    assert stream.content_hash == content_hash_of(REAL_MP4)
    assert stream.filename.endswith(".mp4")


def test_content_stream_refuses_an_operator_artifact() -> None:
    """A harvested log can be CLEAN and still be nothing a learner may fetch (D078)."""
    with pytest.raises(DomainError) as caught:
        content_stream(_record(audience=Audience.OPERATOR), _chunks(), CONTRACT)
    assert caught.value.code is ErrorCode.ARTIFACT_NOT_FOUND


def test_content_stream_refuses_a_quarantined_artifact() -> None:
    with pytest.raises(DomainError) as caught:
        content_stream(_record(scan_verdict=ScanVerdict.QUARANTINED), _chunks(), CONTRACT)
    assert caught.value.code is ErrorCode.ARTIFACT_NOT_FOUND
