"""CORE. Untrusted text in, sealed `LessonBrief` out. See `docs/plan/06-trust-boundary.md`.

Five layers on the way in: edge limits (the api lane), the sanitiser, the guard, the intent
classifier, and sealing. Three of them are built. The guard admits everything and the classifier
does not exist, both cut by `docs/demo.md`, and both say so where they are missing rather than
looking present.

Re-exported explicitly rather than by star, so `__all__` is the seam: `sanitiser`, `sealer`, and
`rendering` are internals of this package and orchestration calls the names below.
"""

from app.intake.ports import (
    GuardPort,
    PermissiveGuard,
    RawConstraints,
    RawContextItem,
    RawLessonRequest,
    TemplateSource,
)
from app.intake.rendering import (
    BRIEF_FILE,
    CONSTRAINTS_FILE,
    CONTEXT_FILE,
    OUTPUT_CONTRACT_FILE,
    PackageTemplateSource,
    fence_for,
    render_bundle,
)
from app.intake.sanitiser import (
    CONTEXT_ITEM_MAX_CHARS,
    INSTRUCTION_MAX_CHARS,
    sanitise,
    sanitise_context,
    sanitise_instruction,
)
from app.intake.sealer import (
    DEFAULT_SUBJECT,
    TEMPLATE_VERSION,
    brief_hash_of,
    brief_record,
    canonical_brief_payload,
    seal_brief,
)

__all__ = [
    "BRIEF_FILE",
    "CONSTRAINTS_FILE",
    "CONTEXT_FILE",
    "CONTEXT_ITEM_MAX_CHARS",
    "DEFAULT_SUBJECT",
    "INSTRUCTION_MAX_CHARS",
    "OUTPUT_CONTRACT_FILE",
    "TEMPLATE_VERSION",
    "GuardPort",
    "PackageTemplateSource",
    "PermissiveGuard",
    "RawConstraints",
    "RawContextItem",
    "RawLessonRequest",
    "TemplateSource",
    "brief_hash_of",
    "brief_record",
    "canonical_brief_payload",
    "fence_for",
    "render_bundle",
    "sanitise",
    "sanitise_context",
    "sanitise_instruction",
    "seal_brief",
]
