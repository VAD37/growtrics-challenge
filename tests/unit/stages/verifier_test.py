"""Independent verification: our checks, our bytes, our verdict (D064).

The worker's own results are a claim and are never used as evidence, so nothing in this module
consults a manifest. What it does assert is the honesty of the chain: a deferred check is
recorded as skipped rather than as a pass, because a verdict built out of checks nobody ran is
worse than no verdict at all.
"""

import zipfile
from io import BytesIO
from typing import Final

import pytest

from app.custody.harvester import content_hash_of
from app.custody.ports import ResultValidator
from app.custody.verifier import (
    CHECKS,
    LOG_LINE_CAP,
    VALIDATOR_VERSION,
    CheckInput,
    ContractResultValidator,
    require_complete,
)
from app.domain.artifact import (
    ArtifactDescriptor,
    CheckOutcome,
    HarvestedFile,
    VerifiedArtifact,
)
from app.domain.contracts import ENABLED_CONTRACTS, contract_for
from app.domain.enums import ArtifactRole, Audience, ProfileId, ScanVerdict
from app.domain.errors import DomainError
from app.generation.backends.mock import SAMPLE_VIDEO_PATH

CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)
REAL_MP4: Final[bytes] = SAMPLE_VIDEO_PATH.read_bytes()


def _harvested(
    data: bytes,
    *,
    role: ArtifactRole = ArtifactRole.PRIMARY,
    media_type: str = "video/mp4",
    rel_path: str = "out/lesson.mp4",
    audience: Audience = Audience.LEARNER,
) -> HarvestedFile:
    return HarvestedFile(
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


def _verify(harvested: HarvestedFile, *, requested_max_duration_s: int = 90) -> VerifiedArtifact:
    return ContractResultValidator().verify(
        harvested, CONTRACT, requested_max_duration_s=requested_max_duration_s
    )


def _outcome(verified: VerifiedArtifact, name: str) -> CheckOutcome:
    return next(check for check in verified.checks if check.name == name)


# --------------------------------------------------------------------------- the port


def test_the_validator_satisfies_its_port() -> None:
    assert isinstance(ContractResultValidator(), ResultValidator)


def test_the_version_is_recorded_on_every_verdict() -> None:
    verified = _verify(_harvested(REAL_MP4))
    assert verified.validator_version == VALIDATOR_VERSION
    assert ContractResultValidator().version == VALIDATOR_VERSION


def test_the_chain_is_the_contract_and_nothing_else() -> None:
    """D077: the checks that run are the ones the worker was handed, in that order."""
    part = next(item for item in CONTRACT.parts if item.role is ArtifactRole.PRIMARY)
    verified = _verify(_harvested(REAL_MP4))
    assert tuple(check.name for check in verified.checks) == part.checks


def test_every_check_named_by_an_enabled_contract_is_registered() -> None:
    """A name in a contract with no entry here would be a silent hole in the acceptance test."""
    named = {
        check
        for contract in ENABLED_CONTRACTS.values()
        for part in contract.parts
        for check in part.checks
    }
    assert named <= set(CHECKS)


# --------------------------------------------------------------------------- what runs


def test_the_real_fixture_passes_the_checks_that_exist() -> None:
    verified = _verify(_harvested(REAL_MP4))
    assert verified.verdict is ScanVerdict.CLEAN
    assert _outcome(verified, "container_is_mp4").passed is True
    assert verified.failed_checks() == ()


def test_a_zip_wearing_an_mp4_name_is_quarantined() -> None:
    """The declared mime says video; the bytes say archive. The bytes win."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("lesson.mp4", "not a video")
    verified = _verify(_harvested(buffer.getvalue()))

    assert verified.verdict is ScanVerdict.QUARANTINED
    assert _outcome(verified, "container_is_mp4").passed is False
    assert "container_is_mp4" in {check.name for check in verified.failed_checks()}


def test_a_truncated_file_is_quarantined_rather_than_crashing_the_check() -> None:
    verified = _verify(_harvested(b"\x00\x00"))
    assert verified.verdict is ScanVerdict.QUARANTINED


def test_a_transcript_that_decodes_is_clean() -> None:
    harvested = _harvested(
        b"why atoms bond\n",
        role=ArtifactRole.TRANSCRIPT,
        media_type="text/plain",
        rel_path="out/transcript.txt",
    )
    verified = _verify(harvested)
    assert _outcome(verified, "utf8_decodes").passed is True
    assert verified.verdict is ScanVerdict.CLEAN


def test_a_transcript_that_is_not_utf8_is_quarantined() -> None:
    harvested = _harvested(
        b"\xff\xfe\x00bad",
        role=ArtifactRole.TRANSCRIPT,
        media_type="text/plain",
        rel_path="out/transcript.txt",
    )
    verified = _verify(harvested)
    assert _outcome(verified, "utf8_decodes").passed is False
    assert verified.verdict is ScanVerdict.QUARANTINED


def test_a_log_over_the_line_cap_is_quarantined() -> None:
    harvested = _harvested(
        b"{}\n" * (LOG_LINE_CAP + 1),
        role=ArtifactRole.LOG,
        media_type="application/x-ndjson",
        rel_path="out/agent.jsonl",
        audience=Audience.OPERATOR,
    )
    verified = _verify(harvested)
    assert _outcome(verified, "line_count_cap").passed is False


def test_a_log_under_the_line_cap_passes() -> None:
    harvested = _harvested(
        b'{"event":"start"}\n',
        role=ArtifactRole.LOG,
        media_type="application/x-ndjson",
        rel_path="out/agent.jsonl",
        audience=Audience.OPERATOR,
    )
    verified = _verify(harvested)
    assert _outcome(verified, "line_count_cap").passed is True


# --------------------------------------------------------------------------- what does not


def test_deferred_checks_are_recorded_as_skipped_and_never_as_passed() -> None:
    """The whole point of the `skipped` flag: an unrun check must not read as evidence."""
    verified = _verify(_harvested(REAL_MP4))
    skipped = {check.name for check in verified.skipped_checks()}
    assert "resolution_at_least_720p" in skipped
    assert "audio_not_silent" in skipped
    for check in verified.skipped_checks():
        assert check.passed is False
        assert check.detail


def test_a_deferred_check_raises_not_implemented_when_called_directly() -> None:
    """One stub test for the deferred half of the chain."""
    part = next(item for item in CONTRACT.parts if item.role is ArtifactRole.PRIMARY)
    with pytest.raises(NotImplementedError):
        CHECKS["audio_not_silent"](
            CheckInput(
                data=REAL_MP4,
                part=part,
                contract=CONTRACT,
                requested_max_duration_s=90,
            )
        )


def test_a_skipped_check_cannot_make_a_verdict_dirty() -> None:
    """Skipped is not failed either. The fixture would fail 720p if that check ran."""
    verified = _verify(_harvested(REAL_MP4))
    assert verified.verdict is ScanVerdict.CLEAN
    assert verified.skipped_checks()


# --------------------------------------------------------------------------- the probe


def test_the_probe_records_what_we_measured() -> None:
    verified = _verify(_harvested(REAL_MP4))
    assert verified.probe["size_bytes"] == len(REAL_MP4)
    assert verified.probe["content_hash"] == content_hash_of(REAL_MP4)
    assert verified.probe["media_type"] == "video/mp4"
    assert verified.probe["validator_version"] == VALIDATOR_VERSION
    assert verified.probe["checks_skipped"] == len(verified.skipped_checks())


# --------------------------------------------------------------------------- completeness


def test_a_set_with_a_clean_primary_is_complete() -> None:
    require_complete((_verify(_harvested(REAL_MP4)),), CONTRACT)


def test_a_set_with_no_primary_is_incomplete() -> None:
    transcript = _verify(
        _harvested(
            b"text",
            role=ArtifactRole.TRANSCRIPT,
            media_type="text/plain",
            rel_path="out/t.txt",
        )
    )
    with pytest.raises(DomainError) as caught:
        require_complete((transcript,), CONTRACT)
    assert caught.value.details["reason"] == "missing_required_part"


def test_a_quarantined_primary_leaves_the_set_incomplete() -> None:
    with pytest.raises(DomainError) as caught:
        require_complete((_verify(_harvested(b"\x00\x00")),), CONTRACT)
    assert caught.value.details["reason"] == "required_part_quarantined"
