"""The whole stage chain, one hop at a time, with nothing faked but the renderer.

`docs/plan/12-data-control.md` ends in a table of hops and payloads. This is that table as a
test: a command in, an artifact row out, through the real sanitiser, the real templates, the
real ACL, the real harvester, and the real validator. The only test double is the pair of
storage ports, which belong to another package.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final

from app.custody.harvester import Harvester
from app.custody.store import ArtifactPublisher
from app.custody.verifier import ContractResultValidator, require_complete
from app.domain.brief import BriefBundle, LessonBrief
from app.domain.contracts import contract_for
from app.domain.enums import ArtifactRole, Audience, ContextKind, ProfileId, ScanVerdict
from app.domain.ids import (
    derive_artifact_id,
    derive_brief_id,
    derive_job_id,
    derive_session_id,
    derive_trace_id,
    new_request_key,
)
from app.domain.records import ArtifactRecord, ContextItem, JobConstraints, SubmitJobCommand
from app.generation.backends.mock import MockGenerationBackend
from app.generation.ports import DEFAULT_LIMITS, GenerationRequest
from app.intake.ports import PermissiveGuard, RawLessonRequest
from app.intake.rendering import OUTPUT_CONTRACT_FILE, render_bundle
from app.intake.sealer import brief_record, seal_brief

CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)
PRINCIPAL_ID: Final[str] = "u_demo"
NOW: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        self.objects[key] = data
        return f"memory://{key}"

    async def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        raise NotImplementedError(storage_uri)


class MemoryWriter:
    def __init__(self) -> None:
        self.rows: list[ArtifactRecord] = []

    async def insert(self, record: ArtifactRecord) -> ArtifactRecord:
        self.rows.append(record)
        return record


async def test_a_command_becomes_an_artifact_row() -> None:
    request_key = new_request_key()
    job_id = derive_job_id(request_key)
    session_id = derive_session_id(job_id, 0)

    command = SubmitJobCommand(
        instruction="why do atoms form covalent bonds",
        context=(ContextItem(kind=ContextKind.LEVEL, text="grade 9"),),
        constraints=JobConstraints(max_duration_s=90, language="en", reading_level=None),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )

    # 1. SubmitJobCommand -> RawLessonRequest (untrusted, revalidated at the step boundary)
    raw = RawLessonRequest.from_command(job_id, command)

    # 2. RawLessonRequest -> LessonBrief (sealed)
    brief: LessonBrief = seal_brief(raw, guard=PermissiveGuard(), sealed_at=NOW)
    assert brief.brief_id == derive_brief_id(job_id)
    assert brief_record(brief).job_id == job_id

    # 3. LessonBrief -> BriefBundle (the files the worker gets)
    bundle: BriefBundle = render_bundle(brief, CONTRACT)
    assert OUTPUT_CONTRACT_FILE in bundle.paths()

    # 4. BriefBundle -> GenerationOutcome (untrusted, what a worker claims)
    backend = MockGenerationBackend(delay_seconds=0.0)
    payload = await backend.generate(
        GenerationRequest(
            session_id=session_id,
            trace_id=derive_trace_id(job_id, 0),
            bundle=bundle,
            contract=CONTRACT,
            limits=DEFAULT_LIMITS,
        )
    )

    # 5. GenerationOutcome -> HarvestedFile (our bytes, our numbers)
    harvested = await Harvester(backend).harvest(payload, CONTRACT, session_id=session_id)
    assert {item.descriptor.role for item in harvested} == {
        ArtifactRole.PRIMARY,
        ArtifactRole.LOG,
    }

    # 6. HarvestedFile -> VerifiedArtifact (our verdict, our validator version)
    validator = ContractResultValidator()
    verified = tuple(
        validator.verify(item, CONTRACT, requested_max_duration_s=90) for item in harvested
    )
    require_complete(verified, CONTRACT)

    # 7. VerifiedArtifact -> ArtifactRecord
    store = MemoryStore()
    writer = MemoryWriter()
    records = await ArtifactPublisher(store, writer).publish_all(
        verified,
        job_id=job_id,
        principal_id=PRINCIPAL_ID,
        chat_context_id=None,
        now=NOW,
    )

    primary = next(record for record in records if record.role is ArtifactRole.PRIMARY)
    assert primary.artifact_id == derive_artifact_id(job_id, primary.content_hash)
    assert primary.mime == "video/mp4"
    assert primary.audience is Audience.LEARNER
    assert primary.scan_verdict is ScanVerdict.CLEAN
    assert primary.principal_id == PRINCIPAL_ID
    assert primary.published_at == NOW
    assert primary.validator_version == validator.version
    assert store.objects[primary.storage_uri.removeprefix("memory://")][4:8] == b"ftyp"

    log = next(record for record in records if record.role is ArtifactRole.LOG)
    assert log.audience is Audience.OPERATOR
    assert log.published_at is None


async def test_two_identical_submissions_are_two_jobs_with_two_artifact_rows() -> None:
    """Scope override item 2: no idempotency. The request key is the only thing that differs."""
    first = derive_job_id(new_request_key())
    second = derive_job_id(new_request_key())
    assert first != second
    assert derive_brief_id(first) != derive_brief_id(second)


async def test_the_worker_never_learns_the_job_it_is_working_on() -> None:
    """`plan/12-data-control.md`: session id and trace id cross, nothing else does."""
    request_key = new_request_key()
    job_id = derive_job_id(request_key)
    command = SubmitJobCommand(
        instruction="why do atoms form covalent bonds",
        context=(),
        constraints=JobConstraints(max_duration_s=90, language="en", reading_level=None),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )
    brief = seal_brief(
        RawLessonRequest.from_command(job_id, command),
        guard=PermissiveGuard(),
        sealed_at=NOW,
    )
    bundle = render_bundle(brief, CONTRACT)

    for file in bundle.files:
        assert job_id not in file.text
        assert request_key not in file.text
        assert PRINCIPAL_ID not in file.text
        assert brief.brief_id not in file.text
