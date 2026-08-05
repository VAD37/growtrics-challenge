"""Anti-corruption layer over the external agent worker. Ports first, backends behind them.

This package writes nothing. It receives a rendered brief bundle, hands it to a backend, and
turns whatever comes back into domain types or a failure code. It never touches the database,
the object store, or principal data (`docs/plan/12-data-control.md`).

Imported by `orchestration.steps` and by `custody.harvester`, and by nothing else.
"""

from app.generation.acl import (
    MAX_PATH_DEPTH,
    accept_descriptors,
    normalise_rel_path,
    parse_outcome,
)
from app.generation.ports import (
    DEFAULT_LIMITS,
    ExecutionLimits,
    GenerationBackend,
    GenerationOutcome,
    GenerationRequest,
    RawArtifactDescriptor,
    RawCheckClaim,
    RawWorkerManifest,
    WorkerStatus,
)

__all__ = [
    "DEFAULT_LIMITS",
    "MAX_PATH_DEPTH",
    "ExecutionLimits",
    "GenerationBackend",
    "GenerationOutcome",
    "GenerationRequest",
    "RawArtifactDescriptor",
    "RawCheckClaim",
    "RawWorkerManifest",
    "WorkerStatus",
    "accept_descriptors",
    "normalise_rel_path",
    "parse_outcome",
]
