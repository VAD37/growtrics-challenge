"""`GenerationBackend` adapters. One exists: the mock.

`local.py`, `sandbox.py`, and `cloud.py` are designed and unbuilt (`docs/plan/07-generation.md`,
`docs/open-questions.md`). Media tech stays deliberately unchosen, so nothing here picks a
renderer, a voice, or an agent template repo.
"""

from app.generation.backends.mock import (
    FAIL_TOKEN,
    LESSON_VIDEOS,
    LOG_REL_PATH,
    POSTER_REL_PATH,
    PRIMARY_REL_PATH,
    SAMPLE_VIDEO_PATH,
    TRANSCRIPT_REL_PATH,
    LessonFixture,
    MockGenerationBackend,
    video_for_brief,
)

__all__ = [
    "FAIL_TOKEN",
    "LESSON_VIDEOS",
    "LOG_REL_PATH",
    "POSTER_REL_PATH",
    "PRIMARY_REL_PATH",
    "SAMPLE_VIDEO_PATH",
    "TRANSCRIPT_REL_PATH",
    "LessonFixture",
    "MockGenerationBackend",
    "video_for_brief",
]
