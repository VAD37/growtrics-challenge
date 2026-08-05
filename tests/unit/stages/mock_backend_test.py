"""The mock backend: a fixture behind the real port, and a fixture that is a real video."""

import time
from datetime import UTC, datetime
from typing import Final

import pytest

from app.domain.contracts import contract_for
from app.domain.enums import ArtifactRole, ProfileId
from app.domain.errors import DomainError
from app.domain.ids import derive_session_id, derive_trace_id
from app.domain.records import JobConstraints, SubmitJobCommand
from app.generation.acl import accept_descriptors, parse_outcome
from app.generation.backends.mock import (
    LOG_REL_PATH,
    MOCK_DELAY_SECONDS,
    PRIMARY_REL_PATH,
    SAMPLE_VIDEO_PATH,
    MockGenerationBackend,
)
from app.generation.ports import (
    DEFAULT_LIMITS,
    GenerationBackend,
    GenerationRequest,
)
from app.intake.ports import PermissiveGuard, RawLessonRequest
from app.intake.rendering import render_bundle
from app.intake.sealer import seal_brief

JOB_ID: Final[str] = "job_" + "0" * 26
CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)
SESSION_ID: Final[str] = derive_session_id(JOB_ID, 0)


def _request() -> GenerationRequest:
    command = SubmitJobCommand(
        instruction="why do atoms form covalent bonds",
        context=(),
        constraints=JobConstraints(max_duration_s=90, language="en", reading_level=None),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )
    brief = seal_brief(
        RawLessonRequest.from_command(JOB_ID, command),
        guard=PermissiveGuard(),
        sealed_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    return GenerationRequest(
        session_id=SESSION_ID,
        trace_id=derive_trace_id(JOB_ID, 0),
        bundle=render_bundle(brief, CONTRACT),
        contract=CONTRACT,
        limits=DEFAULT_LIMITS,
    )


def _backend() -> MockGenerationBackend:
    return MockGenerationBackend(delay_seconds=0.01)


# --------------------------------------------------------------------------- the fixture


def test_the_committed_fixture_is_a_real_mp4() -> None:
    """A renamed text file would pass every test in this file except this one."""
    data = SAMPLE_VIDEO_PATH.read_bytes()
    assert data[4:8] == b"ftyp"
    assert data[8:12] in {b"isom", b"mp42", b"avc1"}
    assert b"moov" in data[:2048] or b"moov" in data[-2048:]
    assert b"mdat" in data
    assert 1024 < len(data) < 1024 * 1024


# --------------------------------------------------------------------------- the port


def test_the_mock_satisfies_the_generation_backend_port() -> None:
    assert isinstance(_backend(), GenerationBackend)


async def test_generate_returns_a_document_the_acl_accepts() -> None:
    """The mock is upstream of the trust boundary, not past it."""
    payload = await _backend().generate(_request())
    accepted = accept_descriptors(parse_outcome(payload), CONTRACT, expected_session_id=SESSION_ID)
    roles = {item.role: item for item in accepted}
    assert roles[ArtifactRole.PRIMARY].rel_path == PRIMARY_REL_PATH
    assert roles[ArtifactRole.PRIMARY].media_type == "video/mp4"
    assert roles[ArtifactRole.LOG].rel_path == LOG_REL_PATH


async def test_declared_sizes_match_what_fetch_will_serve() -> None:
    backend = _backend()
    payload = await backend.generate(_request())
    for descriptor in payload["descriptors"]:  # type: ignore[union-attr]
        data = await backend.fetch(SESSION_ID, descriptor["rel_path"], max_bytes=100 * 1024 * 1024)
        assert len(data) == descriptor["size_bytes"]


async def test_generation_takes_observable_time() -> None:
    """A job that never sits in RUNNING demonstrates nothing about the queue."""
    assert MOCK_DELAY_SECONDS > 0
    backend = MockGenerationBackend(delay_seconds=0.05)
    started = time.perf_counter()
    await backend.generate(_request())
    assert time.perf_counter() - started >= 0.05


# --------------------------------------------------------------------------- fetch


async def test_fetch_serves_the_video_bytes() -> None:
    backend = _backend()
    await backend.generate(_request())
    data = await backend.fetch(SESSION_ID, PRIMARY_REL_PATH, max_bytes=64 * 1024 * 1024)
    assert data == SAMPLE_VIDEO_PATH.read_bytes()


async def test_fetch_refuses_a_path_the_manifest_never_declared() -> None:
    backend = _backend()
    await backend.generate(_request())
    with pytest.raises(DomainError) as caught:
        await backend.fetch(SESSION_ID, "out/../../../etc/passwd", max_bytes=1024)
    assert caught.value.details["reason"] == "unknown_candidate"


async def test_fetch_refuses_a_session_that_was_never_opened() -> None:
    with pytest.raises(DomainError) as caught:
        await _backend().fetch("ses_" + "9" * 26, PRIMARY_REL_PATH, max_bytes=1024)
    assert caught.value.details["reason"] == "unknown_session"


async def test_fetch_stops_one_byte_past_the_cap() -> None:
    """The harvester decides that an oversize file is fatal; the backend just stops reading."""
    backend = _backend()
    await backend.generate(_request())
    data = await backend.fetch(SESSION_ID, PRIMARY_REL_PATH, max_bytes=100)
    assert len(data) == 101


async def test_the_agent_log_carries_the_trace_and_no_learner_text() -> None:
    backend = _backend()
    request = _request()
    await backend.generate(request)
    log = await backend.fetch(SESSION_ID, LOG_REL_PATH, max_bytes=8 * 1024 * 1024)
    text = log.decode("utf-8")
    assert request.trace_id in text
    assert request.bundle.brief_hash in text
    assert "covalent" not in text
    for line in text.splitlines():
        assert line.startswith("{")
