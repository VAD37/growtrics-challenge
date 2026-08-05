"""`GenerationBackend` adapters. One exists: the mock.

`local.py`, `sandbox.py`, and `cloud.py` are designed and unbuilt (`docs/plan/07-generation.md`,
`docs/open-questions.md`). Media tech stays deliberately unchosen, so nothing here picks a
renderer, a voice, or an agent template repo.
"""

from app.generation.backends.mock import (
    LOG_REL_PATH,
    MOCK_DELAY_SECONDS,
    PRIMARY_REL_PATH,
    SAMPLE_VIDEO_PATH,
    MockGenerationBackend,
)

__all__ = [
    "LOG_REL_PATH",
    "MOCK_DELAY_SECONDS",
    "PRIMARY_REL_PATH",
    "SAMPLE_VIDEO_PATH",
    "MockGenerationBackend",
]
