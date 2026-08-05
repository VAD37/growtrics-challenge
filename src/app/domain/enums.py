"""Closed vocabularies.

Text in the database and text in JSON, never an integer whose meaning lives in application
code (D072). Members are added; existing members are never repurposed or removed, only
deprecated. A client that meets an unknown member must tolerate it rather than crash.

`ArtifactRole` and `Audience` are two of the three fields that replaced `Artifact.kind`
(A1, A2, D078); `mime` is the third. One enum could not hold "what is this file for", "what
format is it", and "who may see it" at once, and the third question is the one that keeps a
harvested agent log away from a learner.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageName(StrEnum):
    """Coarse label for humans, mirroring the current step.

    `ADMISSION` and `PLACEMENT` belong to stages the demo does not build, and `FAILED` is not
    a position in the pipeline. All three are frozen members, and none carries a percent.
    """

    INTAKE = "INTAKE"
    ADMISSION = "ADMISSION"
    PLACEMENT = "PLACEMENT"
    PREPARING = "PREPARING"
    GENERATING = "GENERATING"
    COLLECTING = "COLLECTING"
    VERIFYING = "VERIFYING"
    PUBLISHING = "PUBLISHING"
    DONE = "DONE"
    FAILED = "FAILED"


class ArtifactRole(StrEnum):
    """What the file is for. Format lives in `mime`, visibility in `Audience` (A1)."""

    PRIMARY = "PRIMARY"
    POSTER = "POSTER"
    TRANSCRIPT = "TRANSCRIPT"
    CAPTIONS = "CAPTIONS"
    ASSET = "ASSET"
    SOURCE = "SOURCE"
    LOG = "LOG"


class Audience(StrEnum):
    """Who may fetch it. Not a safety verdict: see `ScanVerdict` (A2, D078)."""

    LEARNER = "LEARNER"
    OPERATOR = "OPERATOR"


class ScanVerdict(StrEnum):
    """Is this file dangerous. Not an audience question."""

    CLEAN = "CLEAN"
    QUARANTINED = "QUARANTINED"


class ContextKind(StrEnum):
    """The closed set of labelled inputs a caller may attach to an instruction."""

    MEMORY = "MEMORY"
    LEVEL = "LEVEL"
    PRIOR_TOPIC = "PRIOR_TOPIC"
    MISCONCEPTION = "MISCONCEPTION"
    LANGUAGE = "LANGUAGE"
    NOTE = "NOTE"


class ReadingLevel(StrEnum):
    PRIMARY = "PRIMARY"
    LOWER_SECONDARY = "LOWER_SECONDARY"
    UPPER_SECONDARY = "UPPER_SECONDARY"


class ProfileId(StrEnum):
    """What to make. Resolves server-side to an Output Contract (A4).

    The one vocabulary whose value is not its member name, because a client writes it.
    """

    VIDEO_SHORT_V1 = "video.short.v1"  # built
    HTML_LESSON_V1 = "html.lesson.v1"  # designed, not built


TERMINAL_JOB_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)
"""Invariant 4 of `VideoJob`: no transition leaves one of these."""


DEMO_STAGE_ORDER: Final[tuple[StageName, ...]] = (
    StageName.INTAKE,
    StageName.PREPARING,
    StageName.GENERATING,
    StageName.COLLECTING,
    StageName.VERIFYING,
    StageName.PUBLISHING,
    StageName.DONE,
)
"""The path a demo job walks, in order. Progress is monotonic because this order is."""


STAGE_PERCENT: Final[Mapping[StageName, int]] = MappingProxyType(
    {
        StageName.INTAKE: 10,
        StageName.PREPARING: 25,
        StageName.GENERATING: 60,
        StageName.COLLECTING: 80,
        StageName.VERIFYING: 90,
        StageName.PUBLISHING: 95,
        StageName.DONE: 100,
    }
)
"""Static stage-to-percent map (D092).

Progress comes from here rather than from step records, so the demo gets a monotonic bar
without the checkpoint tables. Restoring real progress means reading `step_records` and
deleting this map; nothing outside `orchestration` writes progress either way.
"""


UNSCORED_STAGES: Final[frozenset[StageName]] = frozenset(set(StageName) - set(STAGE_PERCENT))
"""Frozen stages the demo never enters, so they have no percent. See `percent_for`."""


def percent_for(stage: StageName) -> int:
    """Progress percent for a stage (D092).

    Raises `ValueError` for a stage outside `DEMO_STAGE_ORDER`. Returning a default here would
    put a zero on the bar after it had already reached sixty, and D042 says the bar never
    goes backwards. A caller that lands here has taken a path the demo does not build.
    """
    percent = STAGE_PERCENT.get(stage)
    if percent is None:
        raise ValueError(stage)
    return percent
