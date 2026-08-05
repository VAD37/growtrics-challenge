"""Artifact harvest, verify, store, serve. Holds the only read path the API is allowed to use.

Sole writer of `artifacts` and of the object store (D066). Never writes job status: it returns
verified artifacts and rows, and orchestration decides what they mean.

The path from an agent's output to a learner's download has exactly one module on it, and this
is that module.
"""

from app.custody.harvester import Harvester, content_hash_of
from app.custody.ports import ArtifactStore, ArtifactWriter, CandidateSource, ResultValidator
from app.custody.store import (
    PUBLIC_PREFIX,
    QUARANTINE_PREFIX,
    ArtifactPublisher,
    artifact_record_for,
    content_stream,
    object_key,
)
from app.custody.verifier import (
    CHECKS,
    LOG_LINE_CAP,
    VALIDATOR_VERSION,
    CheckInput,
    ContractResultValidator,
    require_complete,
)

__all__ = [
    "CHECKS",
    "LOG_LINE_CAP",
    "PUBLIC_PREFIX",
    "QUARANTINE_PREFIX",
    "VALIDATOR_VERSION",
    "ArtifactPublisher",
    "ArtifactStore",
    "ArtifactWriter",
    "CandidateSource",
    "CheckInput",
    "ContractResultValidator",
    "Harvester",
    "ResultValidator",
    "artifact_record_for",
    "content_hash_of",
    "content_stream",
    "object_key",
    "require_complete",
]
