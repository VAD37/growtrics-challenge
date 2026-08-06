"""The mock backend: fixtures behind the real port, and fixtures that are real media."""

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from app.config import settings
from app.domain.contracts import contract_for, part_for_media_type
from app.domain.enums import ArtifactRole, ProfileId
from app.domain.errors import DomainError
from app.domain.ids import derive_session_id, derive_trace_id
from app.domain.records import JobConstraints, SubmitJobCommand
from app.generation.acl import accept_descriptors, parse_outcome
from app.generation.backends.mock import (
    FAIL_TOKEN,
    LESSON_VIDEOS,
    LOG_REL_PATH,
    POSTER_REL_PATH,
    PRIMARY_REL_PATH,
    SAMPLE_VIDEO_PATH,
    TRANSCRIPT_REL_PATH,
    MockGenerationBackend,
    video_for_brief,
)
from app.generation.ports import (
    DEFAULT_LIMITS,
    GenerationBackend,
    GenerationRequest,
    WorkerStatus,
)
from app.intake.ports import PermissiveGuard, RawLessonRequest
from app.intake.rendering import render_bundle
from app.intake.sealer import seal_brief

JOB_ID: Final[str] = "job_" + "0" * 26
CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)
SESSION_ID: Final[str] = derive_session_id(JOB_ID, 0)

ROLE_BY_PATH: Final[dict[str, ArtifactRole]] = {
    PRIMARY_REL_PATH: ArtifactRole.PRIMARY,
    POSTER_REL_PATH: ArtifactRole.POSTER,
    TRANSCRIPT_REL_PATH: ArtifactRole.TRANSCRIPT,
    LOG_REL_PATH: ArtifactRole.LOG,
}


def _request(instruction: str = "why do atoms form covalent bonds") -> GenerationRequest:
    command = SubmitJobCommand(
        instruction=instruction,
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
    return MockGenerationBackend(delay_min_seconds=0.0, delay_max_seconds=0.0)


# --------------------------------------------------------------------------- the fixtures


def _is_real_mp4(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and b"mdat" in data and b"moov" in data


@pytest.mark.parametrize("video", LESSON_VIDEOS, ids=lambda item: item.slug)
def test_the_committed_lessons_are_real_mp4s(video) -> None:
    """A renamed text file would pass every test in this file except this one."""
    data = video.path.read_bytes()
    assert _is_real_mp4(data)
    assert data[8:12] in {b"isom", b"mp42", b"avc1"}
    assert 100 * 1024 < len(data) < 8 * 1024 * 1024


@pytest.mark.parametrize("video", LESSON_VIDEOS, ids=lambda item: item.slug)
def test_every_lesson_brings_its_own_poster_and_transcript(video) -> None:
    """One pair shared between all of them would put the wrong narration on the wrong video."""
    assert video.poster_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert video.transcript_path.read_bytes().decode("utf-8").strip()


@pytest.mark.parametrize("video", LESSON_VIDEOS, ids=lambda item: item.slug)
def test_the_engine_result_document_describes_the_bytes_beside_it(video) -> None:
    """The fabricated `result.json` and the committed files drift apart silently otherwise.

    `bytes` and `sha256` are the parts of that document measured off these files rather than
    written by hand, so they are the parts worth pinning. `duration_s` is pinned against the
    catalog's own number, which is what the manifest claims to a client.
    """
    document = json.loads(video.result_path.read_text(encoding="utf-8"))
    by_role = {item["role"]: item for item in document["artifacts"]}
    sources = {
        "video": video.path,
        "transcript": video.transcript_path,
        "poster": video.poster_path,
    }
    for role, source in sources.items():
        data = source.read_bytes()
        assert by_role[role]["bytes"] == len(data), role
        assert by_role[role]["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest(), role
    assert abs(by_role["video"]["duration_s"] - video.duration_s) < 0.1


def test_the_lessons_are_different_files() -> None:
    """The hash pick proves nothing if every branch lands on the same bytes."""
    assert len({video.path.read_bytes() for video in LESSON_VIDEOS}) == len(LESSON_VIDEOS)


def test_the_small_sample_is_kept_for_the_tests_that_only_need_an_ftyp_box() -> None:
    data = SAMPLE_VIDEO_PATH.read_bytes()
    assert _is_real_mp4(data)
    assert len(data) < 64 * 1024


# --------------------------------------------------------------------------- the port


def test_the_mock_satisfies_the_generation_backend_port() -> None:
    assert isinstance(_backend(), GenerationBackend)


async def test_generate_returns_a_document_the_acl_accepts() -> None:
    """The mock is upstream of the trust boundary, not past it."""
    payload = await _backend().generate(_request())
    accepted = accept_descriptors(parse_outcome(payload), CONTRACT, expected_session_id=SESSION_ID)
    by_path = {item.rel_path: item for item in accepted}
    assert set(by_path) == set(ROLE_BY_PATH)
    for rel_path, role in ROLE_BY_PATH.items():
        assert by_path[rel_path].role is role


async def test_every_descriptor_fits_the_part_the_contract_declares() -> None:
    """A media type or a size the contract does not accept fails the run, not the assertion."""
    payload = await _backend().generate(_request())
    descriptors = payload["descriptors"]
    assert len(descriptors) == 4  # type: ignore[arg-type]
    for descriptor in descriptors:  # type: ignore[union-attr]
        part = part_for_media_type(CONTRACT, descriptor["media_type"])
        assert part is not None, descriptor["media_type"]
        assert part.role is ROLE_BY_PATH[descriptor["rel_path"]]
        assert descriptor["size_bytes"] <= part.max_bytes


async def test_declared_sizes_match_what_fetch_will_serve() -> None:
    backend = _backend()
    payload = await backend.generate(_request())
    for descriptor in payload["descriptors"]:  # type: ignore[union-attr]
        data = await backend.fetch(SESSION_ID, descriptor["rel_path"], max_bytes=100 * 1024 * 1024)
        assert len(data) == descriptor["size_bytes"]


# --------------------------------------------------------------------------- which video


def test_the_video_is_a_function_of_the_brief_hash() -> None:
    """Same brief, same lesson, in this process and the next one."""
    for brief_hash in (f"sha256:{index:064x}" for index in range(32)):
        assert video_for_brief(brief_hash) is video_for_brief(brief_hash)


def test_every_lesson_is_reachable() -> None:
    """A pick that always lands on one file is a hardcoded path with arithmetic in front."""
    picked = {video_for_brief(f"sha256:{index:064x}") for index in range(64)}
    assert picked == set(LESSON_VIDEOS)


async def test_two_different_briefs_can_get_two_different_videos() -> None:
    """What proves the bytes came from this job rather than from a constant."""
    backend = _backend()
    served: set[bytes] = set()
    for index in range(32):
        request = _request(f"explain reaction number {index}")
        await backend.generate(request)
        served.add(await backend.fetch(SESSION_ID, PRIMARY_REL_PATH, max_bytes=64 * 1024 * 1024))
    assert len(served) == len(LESSON_VIDEOS)


async def test_the_same_brief_gets_the_same_video_twice() -> None:
    backend = _backend()
    request = _request()
    await backend.generate(request)
    first = await backend.fetch(SESSION_ID, PRIMARY_REL_PATH, max_bytes=64 * 1024 * 1024)
    await backend.generate(request)
    assert await backend.fetch(SESSION_ID, PRIMARY_REL_PATH, max_bytes=64 * 1024 * 1024) == first


# --------------------------------------------------------------------------- the delay


def test_the_configured_delay_is_long_enough_to_watch() -> None:
    """A job that never sits in RUNNING demonstrates nothing about the queue."""
    assert 0 < settings.mock_delay_min_seconds <= settings.mock_delay_max_seconds


def test_the_delay_is_drawn_inside_its_bounds_and_is_not_constant() -> None:
    backend = MockGenerationBackend(delay_min_seconds=2.0, delay_max_seconds=5.0)
    draws = {backend.next_delay_seconds() for _ in range(200)}
    assert all(2.0 <= value <= 5.0 for value in draws)
    assert len(draws) > 1


def test_the_delay_can_be_switched_off_for_a_test() -> None:
    assert _backend().next_delay_seconds() == 0.0


async def test_generation_takes_the_time_it_drew() -> None:
    backend = MockGenerationBackend(delay_min_seconds=0.05, delay_max_seconds=0.05)
    started = time.perf_counter()
    await backend.generate(_request())
    assert time.perf_counter() - started >= 0.05


# --------------------------------------------------------------------------- the failure knob


async def test_the_fail_token_reports_a_failed_run() -> None:
    """One string match, so the failure path can be demonstrated rather than waited for."""
    payload = await _backend().generate(_request(f"melt something {FAIL_TOKEN} please"))
    assert payload["status"] == WorkerStatus.FAILED.value
    assert payload["descriptors"] == []


async def test_the_acl_turns_a_failed_run_into_generation_failed() -> None:
    payload = await _backend().generate(_request(f"{FAIL_TOKEN}"))
    with pytest.raises(DomainError) as caught:
        accept_descriptors(parse_outcome(payload), CONTRACT, expected_session_id=SESSION_ID)
    assert caught.value.details["reason"] == "worker_reported_failure"


async def test_a_failed_run_leaves_no_workspace_to_fetch_from() -> None:
    backend = _backend()
    await backend.generate(_request(f"{FAIL_TOKEN} now"))
    with pytest.raises(DomainError) as caught:
        await backend.fetch(SESSION_ID, PRIMARY_REL_PATH, max_bytes=1024)
    assert caught.value.details["reason"] == "unknown_session"


async def test_an_ordinary_instruction_is_untouched_by_the_knob() -> None:
    """The token is matched and nothing else is. No content is inspected here."""
    payload = await _backend().generate(_request("what makes an acid fail me on a test"))
    assert payload["status"] == WorkerStatus.COMPLETED.value


# --------------------------------------------------------------------------- fetch


async def test_fetch_serves_the_fixture_bytes_unchanged() -> None:
    backend = _backend()
    request = _request()
    await backend.generate(request)

    lesson = video_for_brief(request.bundle.brief_hash)
    expected: dict[str, Path] = {
        PRIMARY_REL_PATH: lesson.path,
        POSTER_REL_PATH: lesson.poster_path,
        TRANSCRIPT_REL_PATH: lesson.transcript_path,
    }
    for rel_path, source in expected.items():
        data = await backend.fetch(SESSION_ID, rel_path, max_bytes=64 * 1024 * 1024)
        assert data == source.read_bytes(), rel_path


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
    # A word no fixture name can contain, because the log does name the fixture it served and
    # one of those fixtures is called `covalent_bonds`.
    request = _request("explain why bleach smells the way it does")
    await backend.generate(request)
    log = await backend.fetch(SESSION_ID, LOG_REL_PATH, max_bytes=8 * 1024 * 1024)
    text = log.decode("utf-8")
    assert request.trace_id in text
    assert request.bundle.brief_hash in text
    assert "bleach" not in text
    for line in text.splitlines():
        assert line.startswith("{")
