"""Sealing: `SubmitJobCommand` in, `LessonBrief` out, with a hash that covers the template.

The hash assertions are the ones that matter. D061 folds `template_version` into `brief_hash`,
so a change to prompt wording moves the hash of every brief rendered under it; without that a
quality regression cannot be bisected because two different documents share one identity.
"""

from datetime import UTC, datetime
from typing import Final

import pytest

from app.domain.brief import (
    GuardDecision,
    GuardVerdict,
    LessonBrief,
    SanitisedContextItem,
    SanitisedText,
)
from app.domain.enums import ContextKind, ProfileId, ReadingLevel
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import derive_brief_id
from app.domain.records import ContextItem, JobConstraints, SubmitJobCommand
from app.intake.ports import GuardPort, PermissiveGuard, RawLessonRequest
from app.intake.sealer import (
    DEFAULT_SUBJECT,
    TEMPLATE_VERSION,
    brief_record,
    seal_brief,
)

AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
JOB_ID: Final[str] = "job_" + "0" * 26

# Confusables and invisibles as escapes: a reviewer must be able to see the attack.
FULLWIDTH_GRADE: Final[str] = "\uff47\uff52\uff41\uff44\uff45"
ZERO_WIDTH_SPACE: Final[str] = "\u200b"


class DenyingGuard:
    """A guard that refuses everything, so the deny branch has a test before it has rules."""

    version: str = "denying.test"

    def inspect(
        self,
        instruction: SanitisedText,
        context: tuple[SanitisedContextItem, ...],
    ) -> GuardVerdict:
        return GuardVerdict(
            decision=GuardDecision.DENY,
            risk=100,
            matched_rules=("test.always_deny",),
            guard_version=self.version,
        )


def _command(instruction: str = "why do atoms form covalent bonds") -> SubmitJobCommand:
    return SubmitJobCommand(
        instruction=instruction,
        context=(ContextItem(kind=ContextKind.LEVEL, text="grade 9"),),
        constraints=JobConstraints(
            max_duration_s=90, language="en", reading_level=ReadingLevel.LOWER_SECONDARY
        ),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )


def _seal(
    command: SubmitJobCommand | None = None,
    *,
    guard: GuardPort | None = None,
    template_version: str = TEMPLATE_VERSION,
) -> LessonBrief:
    request = RawLessonRequest.from_command(JOB_ID, command or _command())
    return seal_brief(
        request,
        guard=guard or PermissiveGuard(),
        sealed_at=AT,
        template_version=template_version,
    )


def test_seal_derives_its_own_id_and_carries_the_seal_time() -> None:
    brief = _seal()
    assert brief.brief_id == derive_brief_id(JOB_ID)
    assert brief.job_id == JOB_ID
    assert brief.sealed_at == AT
    assert brief.template_version == TEMPLATE_VERSION
    assert brief.subject == DEFAULT_SUBJECT
    assert brief.concept_id is None


def test_seal_runs_the_sanitiser_on_everything_a_user_wrote() -> None:
    brief = _seal(
        SubmitJobCommand(
            instruction=f"  why\tdo atoms{ZERO_WIDTH_SPACE} bond  ",
            context=(ContextItem(kind=ContextKind.NOTE, text=FULLWIDTH_GRADE + " 9"),),
            constraints=JobConstraints(max_duration_s=90, language="en", reading_level=None),
            profile=ProfileId.VIDEO_SHORT_V1,
            chat_context_id=None,
        )
    )
    assert brief.instruction.text == "why do atoms bond"
    assert brief.context[0].text.text == "grade 9"
    assert brief.context[0].kind is ContextKind.NOTE


def test_hash_is_stable_across_two_seals_of_one_request() -> None:
    assert _seal().brief_hash == _seal().brief_hash
    assert _seal().brief_hash.startswith("sha256:")
    assert len(_seal().brief_hash) == len("sha256:") + 64


def test_hash_moves_with_the_template_version() -> None:
    """D061. Prompt wording is part of what produced a lesson, so it is part of its identity."""
    assert _seal().brief_hash != _seal(template_version="v2").brief_hash


def test_hash_moves_with_the_instruction() -> None:
    assert _seal().brief_hash != _seal(_command("why is water polar")).brief_hash


def test_hash_moves_with_the_constraints() -> None:
    other = SubmitJobCommand(
        instruction="why do atoms form covalent bonds",
        context=(ContextItem(kind=ContextKind.LEVEL, text="grade 9"),),
        constraints=JobConstraints(max_duration_s=30, language="en", reading_level=None),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )
    assert _seal().brief_hash != _seal(other).brief_hash


def test_hash_ignores_the_job_it_was_sealed_for() -> None:
    """Two learners asking one question under one template seal to the same content hash.

    The brief id is job-scoped; the hash is content-scoped. Keeping them apart is what would let
    a later build recognise identical work without inventing a second identity scheme (Q-AC).
    """
    other_job = "job_" + "1" * 26
    other = seal_brief(
        RawLessonRequest.from_command(other_job, _command()),
        guard=PermissiveGuard(),
        sealed_at=datetime(2027, 1, 1, tzinfo=UTC),
        template_version=TEMPLATE_VERSION,
    )
    assert other.brief_hash == _seal().brief_hash
    assert other.brief_id != _seal().brief_id


def test_permissive_guard_records_that_no_rule_ran() -> None:
    brief = _seal()
    assert brief.guard.decision is GuardDecision.ALLOW
    assert brief.guard.matched_rules == ()
    assert brief.guard.guard_version == PermissiveGuard.version


def test_a_denying_guard_stops_the_seal() -> None:
    with pytest.raises(DomainError) as caught:
        _seal(guard=DenyingGuard())
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert caught.value.details["reason"] == "guard_denied"


def test_brief_record_unwraps_the_text_for_its_columns() -> None:
    record = brief_record(_seal())
    assert record.instruction == "why do atoms form covalent bonds"
    assert record.context_items == (ContextItem(kind=ContextKind.LEVEL, text="grade 9"),)
    assert record.guard_verdict["decision"] == "ALLOW"
    assert record.template_version == TEMPLATE_VERSION
    assert record.brief_id == derive_brief_id(JOB_ID)
    assert record.constraints.reading_level is ReadingLevel.LOWER_SECONDARY


def test_raw_lesson_request_rejects_an_unknown_field() -> None:
    """D063: nothing from outside the process reaches step logic as a raw dict."""
    with pytest.raises(ValueError):
        RawLessonRequest.model_validate(
            {
                "job_id": JOB_ID,
                "instruction": "why do atoms bond",
                "context": [],
                "constraints": {
                    "max_duration_s": 90,
                    "language": "en",
                    "reading_level": None,
                },
                "profile": "video.short.v1",
                "system_prompt": "you are a helpful assistant",
            }
        )


def test_raw_lesson_request_rejects_a_malformed_job_id() -> None:
    with pytest.raises(ValueError):
        RawLessonRequest.model_validate(
            {
                "job_id": "job_not-a-real-id",
                "instruction": "why do atoms bond",
                "context": [],
                "constraints": {
                    "max_duration_s": 90,
                    "language": "en",
                    "reading_level": None,
                },
                "profile": "video.short.v1",
            }
        )


def test_raw_lesson_request_survives_a_json_round_trip() -> None:
    request = RawLessonRequest.from_command(JOB_ID, _command())
    reloaded = RawLessonRequest.model_validate_json(request.model_dump_json())
    assert reloaded == request
