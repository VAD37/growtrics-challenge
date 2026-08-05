"""Closed vocabularies. Values are the wire format, so membership is a contract (D072)."""

from enum import StrEnum

import pytest

from app.domain.enums import (
    DEMO_STAGE_ORDER,
    STAGE_PERCENT,
    TERMINAL_JOB_STATUSES,
    UNSCORED_STAGES,
    ArtifactRole,
    Audience,
    ContextKind,
    JobStatus,
    ProfileId,
    ReadingLevel,
    ScanVerdict,
    StageName,
    percent_for,
)

ALL_ENUMS: list[type[StrEnum]] = [
    JobStatus,
    StageName,
    ArtifactRole,
    Audience,
    ContextKind,
    ReadingLevel,
    ProfileId,
    ScanVerdict,
]


# --------------------------------------------------------------------------- shape


@pytest.mark.parametrize("enum_type", ALL_ENUMS)
def test_every_vocabulary_is_a_string_enum(enum_type: type[StrEnum]) -> None:
    assert issubclass(enum_type, StrEnum)
    assert len(enum_type) == len({member.value for member in enum_type})


@pytest.mark.parametrize(
    "enum_type",
    [JobStatus, StageName, ArtifactRole, Audience, ContextKind, ReadingLevel, ScanVerdict],
)
def test_member_names_are_their_wire_values(enum_type: type[StrEnum]) -> None:
    for member in enum_type:
        assert member.value == member.name


def test_profile_ids_are_dotted_and_versioned() -> None:
    # The one vocabulary whose value is not its name: a client asks for "video.short.v1".
    assert ProfileId.VIDEO_SHORT_V1.value == "video.short.v1"
    assert ProfileId.HTML_LESSON_V1.value == "html.lesson.v1"


# --------------------------------------------------------------------------- membership


def test_job_statuses() -> None:
    assert {member.value for member in JobStatus} == {
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }


def test_stage_names() -> None:
    assert {member.value for member in StageName} == {
        "INTAKE",
        "ADMISSION",
        "PLACEMENT",
        "PREPARING",
        "GENERATING",
        "COLLECTING",
        "VERIFYING",
        "PUBLISHING",
        "DONE",
        "FAILED",
    }


def test_artifact_roles() -> None:
    # A1: `VIDEO` is gone, `PRIMARY` and `CAPTIONS` and `ASSET` are in.
    assert {member.value for member in ArtifactRole} == {
        "PRIMARY",
        "POSTER",
        "TRANSCRIPT",
        "CAPTIONS",
        "ASSET",
        "SOURCE",
        "LOG",
    }
    assert "VIDEO" not in {member.value for member in ArtifactRole}


def test_audience_and_scan_verdict_are_different_questions() -> None:
    # D078: `scan_verdict` answers "is this dangerous", `audience` answers "is this theirs".
    assert {member.value for member in Audience} == {"LEARNER", "OPERATOR"}
    assert {member.value for member in ScanVerdict} == {"CLEAN", "QUARANTINED"}
    assert not {member.value for member in Audience} & {member.value for member in ScanVerdict}


def test_context_kinds() -> None:
    assert {member.value for member in ContextKind} == {
        "MEMORY",
        "LEVEL",
        "PRIOR_TOPIC",
        "MISCONCEPTION",
        "LANGUAGE",
        "NOTE",
    }


def test_reading_levels() -> None:
    assert {member.value for member in ReadingLevel} == {
        "PRIMARY",
        "LOWER_SECONDARY",
        "UPPER_SECONDARY",
    }


def test_terminal_statuses_are_the_three_that_never_transition() -> None:
    # Invariant 4 of VideoJob.
    assert (
        frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
        == TERMINAL_JOB_STATUSES
    )
    assert JobStatus.QUEUED not in TERMINAL_JOB_STATUSES
    assert JobStatus.RUNNING not in TERMINAL_JOB_STATUSES


# --------------------------------------------------------------------------- progress map


def test_stage_percent_is_exactly_the_map_in_the_demo() -> None:
    assert dict(STAGE_PERCENT) == {
        StageName.INTAKE: 10,
        StageName.PREPARING: 25,
        StageName.GENERATING: 60,
        StageName.COLLECTING: 80,
        StageName.VERIFYING: 90,
        StageName.PUBLISHING: 95,
        StageName.DONE: 100,
    }


def test_the_demo_order_is_every_stage_the_demo_can_reach() -> None:
    assert set(DEMO_STAGE_ORDER) == set(STAGE_PERCENT)
    assert len(DEMO_STAGE_ORDER) == len(set(DEMO_STAGE_ORDER))


def test_percent_is_strictly_monotonic_in_the_demo_stage_order() -> None:
    # D042: a bar that reverses reads as broken even when recovery is working.
    percents = [STAGE_PERCENT[stage] for stage in DEMO_STAGE_ORDER]
    assert percents == sorted(percents)
    assert len(set(percents)) == len(percents)


def test_percent_stays_inside_the_progress_view_bounds() -> None:
    for percent in STAGE_PERCENT.values():
        assert 0 <= percent <= 100


def test_the_run_starts_at_intake_and_ends_at_a_hundred() -> None:
    assert DEMO_STAGE_ORDER[0] is StageName.INTAKE
    assert DEMO_STAGE_ORDER[-1] is StageName.DONE
    assert STAGE_PERCENT[StageName.DONE] == 100


def test_stage_percent_is_read_only() -> None:
    with pytest.raises(TypeError):
        STAGE_PERCENT[StageName.DONE] = 0  # type: ignore[index]


@pytest.mark.parametrize("stage", list(DEMO_STAGE_ORDER))
def test_percent_for_answers_every_reachable_stage(stage: StageName) -> None:
    assert percent_for(stage) == STAGE_PERCENT[stage]


def test_unscored_stages_are_the_ones_the_demo_never_enters() -> None:
    assert (
        frozenset({StageName.ADMISSION, StageName.PLACEMENT, StageName.FAILED}) == UNSCORED_STAGES
    )
    assert not UNSCORED_STAGES & set(STAGE_PERCENT)
    assert UNSCORED_STAGES | set(STAGE_PERCENT) == set(StageName)


@pytest.mark.parametrize("stage", [StageName.ADMISSION, StageName.PLACEMENT, StageName.FAILED])
def test_percent_for_refuses_a_stage_with_no_percent(stage: StageName) -> None:
    # Better a loud programming error than a silent zero, which would reverse the bar.
    with pytest.raises(ValueError):
        percent_for(stage)
