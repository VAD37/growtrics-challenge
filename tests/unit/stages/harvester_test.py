"""Harvest: pull the declared candidates, measure them ourselves, believe nothing.

The worker's numbers are recorded and never used as evidence. Every assertion here is about the
difference between what was claimed and what arrived.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

import pytest

from app.custody.harvester import Harvester, content_hash_of
from app.custody.ports import CandidateSource
from app.domain.artifact import HarvestedFile
from app.domain.contracts import contract_for
from app.domain.enums import ArtifactRole, ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import derive_session_id, derive_trace_id
from app.domain.records import JobConstraints, SubmitJobCommand
from app.generation.backends.mock import (
    LOG_REL_PATH,
    PRIMARY_REL_PATH,
    MockGenerationBackend,
)
from app.generation.ports import DEFAULT_LIMITS, GenerationRequest
from app.intake.ports import PermissiveGuard, RawLessonRequest
from app.intake.rendering import render_bundle
from app.intake.sealer import seal_brief

JOB_ID: Final[str] = "job_" + "0" * 26
SESSION_ID: Final[str] = derive_session_id(JOB_ID, 0)
CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)
MIB: Final[int] = 1024 * 1024


def generation_request() -> GenerationRequest:
    """A real request, so the end-to-end test drives the same seam the worker does."""
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


class FakeSource:
    """A workspace we control, so a lying manifest is one dictionary away."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files: Mapping[str, bytes] = files
        self.requested: list[tuple[str, int]] = []

    async def fetch(self, session_id: str, rel_path: str, *, max_bytes: int) -> bytes:
        self.requested.append((rel_path, max_bytes))
        data = self.files.get(rel_path)
        if data is None:
            raise DomainError(ErrorCode.GENERATION_FAILED, {"reason": "unknown_candidate"})
        return data[: max_bytes + 1]


def _payload(descriptors: list[Mapping[str, object]]) -> Mapping[str, object]:
    return {
        "session_id": SESSION_ID,
        "status": "COMPLETED",
        "descriptors": descriptors,
        "manifest": {"profile": "video.short.v1", "contract_version": "v1"},
    }


def _primary(size_bytes: int, *, sha256: str | None = None) -> Mapping[str, object]:
    return {
        "role": "PRIMARY",
        "media_type": "video/mp4",
        "rel_path": PRIMARY_REL_PATH,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


async def _harvest(
    source: CandidateSource,
    payload: Mapping[str, object],
) -> tuple[HarvestedFile, ...]:
    return await Harvester(source).harvest(payload, CONTRACT, session_id=SESSION_ID)


# --------------------------------------------------------------------------- hashing


def test_content_hash_is_prefixed_and_lowercase() -> None:
    digest = content_hash_of(b"")
    assert digest == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# --------------------------------------------------------------------------- measurement


async def test_harvest_measures_size_and_hash_itself() -> None:
    data = b"\x00\x00\x00\x20ftypisom" + b"x" * 100
    source = FakeSource({PRIMARY_REL_PATH: data})

    harvested = await _harvest(source, _payload([_primary(len(data))]))

    assert len(harvested) == 1
    assert harvested[0].size_bytes == len(data)
    assert harvested[0].content_hash == content_hash_of(data)
    assert harvested[0].descriptor.role is ArtifactRole.PRIMARY
    assert harvested[0].claim_agreed is True


async def test_a_size_the_worker_lied_about_is_recorded_not_trusted() -> None:
    """A claim that disagrees with reality is a signal, not a reason to drop the file."""
    data = b"\x00\x00\x00\x20ftypisom" + b"x" * 100
    source = FakeSource({PRIMARY_REL_PATH: data})

    harvested = await _harvest(source, _payload([_primary(999_999)]))

    assert harvested[0].size_bytes == len(data)
    assert harvested[0].descriptor.declared_size_bytes == 999_999
    assert harvested[0].claim_agreed is False


async def test_a_hash_the_worker_lied_about_is_recorded_not_trusted() -> None:
    data = b"\x00\x00\x00\x20ftypisom"
    source = FakeSource({PRIMARY_REL_PATH: data})

    harvested = await _harvest(source, _payload([_primary(len(data), sha256="0" * 64)]))

    assert harvested[0].content_hash == content_hash_of(data)
    assert harvested[0].claim_agreed is False


async def test_a_matching_hash_claim_agrees() -> None:
    data = b"\x00\x00\x00\x20ftypisom"
    digest = content_hash_of(data).removeprefix("sha256:")
    source = FakeSource({PRIMARY_REL_PATH: data})

    harvested = await _harvest(source, _payload([_primary(len(data), sha256=digest)]))

    assert harvested[0].claim_agreed is True


# --------------------------------------------------------------------------- caps


async def test_the_part_cap_is_passed_to_the_source_and_enforced_on_return() -> None:
    """Checked while streaming rather than after: a file that keeps producing bytes is cut."""
    part = next(item for item in CONTRACT.parts if item.role is ArtifactRole.PRIMARY)
    source = FakeSource({PRIMARY_REL_PATH: b"x" * (part.max_bytes + 10)})

    with pytest.raises(DomainError) as caught:
        await _harvest(source, _payload([_primary(10)]))

    assert caught.value.details["reason"] == "size_over_cap"
    assert source.requested == [(PRIMARY_REL_PATH, part.max_bytes)]


async def test_a_total_over_the_contract_cap_is_rejected() -> None:
    """64 MiB of video plus three 8 MiB logs is 88 MiB, and the contract allows 80."""
    source = FakeSource(
        {
            PRIMARY_REL_PATH: b"x" * (64 * MIB),
            "out/a.jsonl": b"y" * (8 * MIB),
            "out/b.jsonl": b"z" * (8 * MIB),
            "out/c.jsonl": b"w" * (8 * MIB),
        }
    )
    payload = _payload(
        [
            _primary(64 * MIB),
            {
                "role": "LOG",
                "media_type": "application/x-ndjson",
                "rel_path": "out/a.jsonl",
                "size_bytes": 1,
                "sha256": None,
            },
            {
                "role": "LOG",
                "media_type": "application/x-ndjson",
                "rel_path": "out/b.jsonl",
                "size_bytes": 1,
                "sha256": None,
            },
            {
                "role": "LOG",
                "media_type": "application/x-ndjson",
                "rel_path": "out/c.jsonl",
                "size_bytes": 1,
                "sha256": None,
            },
        ]
    )

    with pytest.raises(DomainError) as caught:
        await _harvest(source, payload)
    assert caught.value.details["reason"] == "total_over_cap"


async def test_an_empty_candidate_is_rejected() -> None:
    source = FakeSource({PRIMARY_REL_PATH: b""})
    with pytest.raises(DomainError) as caught:
        await _harvest(source, _payload([_primary(0)]))
    assert caught.value.details["reason"] == "empty_candidate"


async def test_a_candidate_the_source_cannot_serve_fails_the_harvest() -> None:
    source = FakeSource({})
    with pytest.raises(DomainError) as caught:
        await _harvest(source, _payload([_primary(10)]))
    assert caught.value.code is ErrorCode.GENERATION_FAILED


# --------------------------------------------------------------------------- the seam


async def test_harvest_refuses_a_hostile_path_before_it_reaches_the_source() -> None:
    """The allowlist runs in the ACL, so the source is never asked for `../../etc/passwd`."""
    source = FakeSource({PRIMARY_REL_PATH: b"x"})
    payload = _payload(
        [
            {
                "role": "PRIMARY",
                "media_type": "video/mp4",
                "rel_path": "out/../../etc/passwd",
                "size_bytes": 10,
                "sha256": None,
            }
        ]
    )

    with pytest.raises(DomainError) as caught:
        await _harvest(source, payload)
    assert caught.value.details["reason"] == "parent_traversal"
    assert source.requested == []


async def test_harvest_runs_end_to_end_against_the_mock_backend() -> None:
    """The one test that exercises the real seam: mock backend, real ACL, real harvest."""
    backend = MockGenerationBackend(delay_seconds=0.0)
    request: GenerationRequest = generation_request()
    payload = await backend.generate(request)

    harvested = await Harvester(backend).harvest(payload, CONTRACT, session_id=request.session_id)

    by_role = {item.descriptor.role: item for item in harvested}
    assert set(by_role) == {ArtifactRole.PRIMARY, ArtifactRole.LOG}
    assert by_role[ArtifactRole.PRIMARY].data[4:8] == b"ftyp"
    assert by_role[ArtifactRole.PRIMARY].claim_agreed is True
    assert by_role[ArtifactRole.LOG].descriptor.rel_path == LOG_REL_PATH
