"""The sealed middle of the stage chain: `SanitisedText`, `LessonBrief`, `BriefBundle`.

Two properties are worth a test each. `SanitisedText` cannot be built by anybody who did not
run the sanitiser, so "this text was cleaned" is a type rather than a convention. The records
around it are frozen, so a sealed brief handed to the renderer cannot come back edited.
"""

from dataclasses import FrozenInstanceError, is_dataclass
from datetime import UTC, datetime
from typing import Final

import pytest

from app.domain.artifact import (
    ArtifactDescriptor,
    CheckOutcome,
    HarvestedFile,
    VerifiedArtifact,
)
from app.domain.brief import (
    BriefBundle,
    BundleFile,
    GuardDecision,
    GuardVerdict,
    LessonBrief,
    SanitisedContextItem,
    SanitisedText,
    guard_verdict_document,
)
from app.domain.enums import ArtifactRole, Audience, ContextKind, ProfileId, ScanVerdict
from app.domain.errors import DomainError, ErrorCode
from app.domain.records import JobConstraints

AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
JOB_ID: Final[str] = "job_" + "0" * 26
BRIEF_ID: Final[str] = "brf_" + "0" * 26


def _text(value: str = "why do atoms form covalent bonds") -> SanitisedText:
    return SanitisedText._mint(value, original_length=len(value), truncated=False)


# --------------------------------------------------------------------------- SanitisedText


def test_sanitised_text_cannot_be_constructed_directly() -> None:
    """The mint token is the guarantee. Without it there is no way to fake a cleaned string."""
    with pytest.raises(DomainError) as caught:
        SanitisedText(text="raw", original_length=3, truncated=False)
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_sanitised_text_keeps_the_original_length_so_truncation_is_visible() -> None:
    minted = SanitisedText._mint("short", original_length=900, truncated=True)
    assert minted.text == "short"
    assert minted.original_length == 900
    assert minted.truncated is True
    assert len(minted.text) != minted.original_length


def test_sanitised_text_is_frozen_and_reads_as_its_text() -> None:
    minted = _text("grade 9")
    with pytest.raises(FrozenInstanceError):
        minted.text = "grade 12"  # type: ignore[misc]
    assert str(minted) == "grade 9"


def test_sanitised_text_rejects_a_negative_original_length() -> None:
    with pytest.raises(DomainError):
        SanitisedText._mint("text", original_length=-1, truncated=False)


def test_sanitised_text_allows_a_result_longer_than_the_input() -> None:
    """NFKC expands as well as contracts, so the length is a record, not a bound."""
    minted = SanitisedText._mint("...", original_length=1, truncated=False)
    assert len(minted) == 3
    assert minted.original_length == 1


# --------------------------------------------------------------------------- guard verdict


def test_guard_verdict_document_is_json_shaped() -> None:
    verdict = GuardVerdict(
        decision=GuardDecision.ALLOW,
        risk=0,
        matched_rules=(),
        guard_version="permissive.v0",
    )
    document = guard_verdict_document(verdict)
    assert document == {
        "decision": "ALLOW",
        "risk": 0,
        "matched_rules": [],
        "guard_version": "permissive.v0",
    }


def test_guard_decision_members_are_the_three_outcomes() -> None:
    assert {member.value for member in GuardDecision} == {"ALLOW", "REVIEW", "DENY"}


# --------------------------------------------------------------------------- LessonBrief


def _brief() -> LessonBrief:
    return LessonBrief(
        brief_id=BRIEF_ID,
        job_id=JOB_ID,
        subject="chemistry",
        concept_id=None,
        instruction=_text(),
        context=(SanitisedContextItem(kind=ContextKind.LEVEL, text=_text("grade 9")),),
        constraints=JobConstraints(max_duration_s=90, language="en", reading_level=None),
        profile=ProfileId.VIDEO_SHORT_V1,
        guard=GuardVerdict(
            decision=GuardDecision.ALLOW,
            risk=0,
            matched_rules=(),
            guard_version="permissive.v0",
        ),
        template_version="v1",
        brief_hash="sha256:" + "0" * 64,
        sealed_at=AT,
    )


def test_lesson_brief_is_frozen_and_carries_the_seal() -> None:
    brief = _brief()
    assert is_dataclass(brief)
    assert brief.template_version == "v1"
    assert brief.brief_hash.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        brief.subject = "physics"  # type: ignore[misc]


def test_brief_bundle_exposes_its_files_by_path() -> None:
    bundle = BriefBundle(
        brief_id=BRIEF_ID,
        template_version="v1",
        brief_hash="sha256:" + "0" * 64,
        files=(
            BundleFile(path="BRIEF.md", text="# Lesson brief\n"),
            BundleFile(path="OUTPUT_CONTRACT.json", text="{}\n"),
        ),
    )
    assert bundle.paths() == ("BRIEF.md", "OUTPUT_CONTRACT.json")
    assert bundle.file("BRIEF.md").text.startswith("# Lesson brief")
    with pytest.raises(DomainError) as caught:
        bundle.file("SYSTEM_PROMPT.md")
    assert caught.value.code is ErrorCode.INVALID_REQUEST


# --------------------------------------------------------------------------- custody records


def _descriptor() -> ArtifactDescriptor:
    return ArtifactDescriptor(
        role=ArtifactRole.PRIMARY,
        audience=Audience.LEARNER,
        media_type="video/mp4",
        rel_path="out/lesson.mp4",
        declared_size_bytes=1024,
        declared_sha256=None,
    )


def test_harvested_file_measures_rather_than_believes() -> None:
    """The declared size is a claim; `size_bytes` is what we counted (`plan/06`)."""
    harvested = HarvestedFile(
        descriptor=_descriptor(),
        data=b"\x00" * 512,
        size_bytes=512,
        content_hash="sha256:" + "a" * 64,
        claim_agreed=False,
    )
    assert harvested.descriptor.declared_size_bytes == 1024
    assert harvested.size_bytes == 512
    assert harvested.claim_agreed is False


def test_verified_artifact_records_skipped_checks_separately_from_failures() -> None:
    verified = VerifiedArtifact(
        harvested=HarvestedFile(
            descriptor=_descriptor(),
            data=b"\x00",
            size_bytes=1,
            content_hash="sha256:" + "a" * 64,
            claim_agreed=True,
        ),
        verdict=ScanVerdict.CLEAN,
        checks=(
            CheckOutcome(name="container_is_mp4", passed=True, skipped=False, detail=""),
            CheckOutcome(name="no_blank_frames", passed=False, skipped=True, detail="deferred"),
        ),
        validator_version="custody.v1",
        probe={"container": "mp4"},
    )
    assert verified.failed_checks() == ()
    assert tuple(check.name for check in verified.skipped_checks()) == ("no_blank_frames",)
    assert verified.verdict is ScanVerdict.CLEAN


def test_check_outcome_cannot_be_passed_and_skipped_at_once() -> None:
    """A skipped check is not evidence of anything, so it may not claim to have passed."""
    with pytest.raises(DomainError):
        CheckOutcome(name="no_blank_frames", passed=True, skipped=True, detail="")
