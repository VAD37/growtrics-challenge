"""Layer 5 on the way in: build, canonicalise, hash, freeze.

From here the exact bytes that will reach a worker are known and reproducible. The hash covers
the sealed content **and** `template_version` (D061), so a change to prompt wording moves the
identity of every brief rendered under it: a quality regression can be bisected by re-rendering
an old brief under a new template, and two documents can never share one hash.

The hash deliberately does not cover the job. Two learners asking one question under one
template seal to the same content hash, which is the fact a later build would need to recognise
identical work (Q-AC); the brief *id* is job-scoped and keeps the rows apart in the meantime.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from app.domain.brief import (
    GuardDecision,
    GuardVerdict,
    LessonBrief,
    guard_verdict_document,
)
from app.domain.enums import ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import derive_brief_id
from app.domain.records import BriefRecord, ContextItem
from app.intake.ports import GuardPort, RawLessonRequest
from app.intake.sanitiser import sanitise_context, sanitise_instruction

TEMPLATE_VERSION: Final[str] = "v1"
"""The template pack in `intake/templates/`. Part of the brief hash (D061)."""

DEFAULT_SUBJECT: Final[str] = "chemistry"
"""`briefs.subject` is NOT NULL and the classifier that would fill it is cut from the demo.

@TODO intent classifier and concept registry, deny by default
(`docs/plan/06-trust-boundary.md`, layer 4). Until then every brief claims the one subject this
service was built for, and `concept_id` stays null rather than being guessed. A request about
something else is sealed and generated rather than refused, which is the gap this constant
names.
"""

_HASH_PREFIX: Final[str] = "sha256:"


def canonical_brief_payload(
    request: RawLessonRequest,
    instruction: str,
    context: tuple[ContextItem, ...],
    template_version: str,
) -> Mapping[str, object]:
    """Everything the hash covers, in one ordered mapping.

    Written out rather than derived from the record so that adding a field to `LessonBrief` does
    not silently change every hash in the database. Widening what is hashed is a deliberate edit
    here, and it is a `template_version` bump.
    """
    return {
        "template_version": template_version,
        "profile": request.profile.value,
        "subject": DEFAULT_SUBJECT,
        "instruction": instruction,
        "context": [{"kind": item.kind.value, "text": item.text} for item in context],
        "constraints": {
            "max_duration_s": request.constraints.max_duration_s,
            "language": request.constraints.language,
            "reading_level": (
                request.constraints.reading_level.value
                if request.constraints.reading_level is not None
                else None
            ),
        },
    }


def brief_hash_of(payload: Mapping[str, object]) -> str:
    """`sha256:` over canonical JSON: sorted keys, no spaces, UTF-8, no ASCII escaping.

    Canonical because two encodings of one document must not produce two hashes. `sort_keys`
    removes insertion order, the compact separators remove whitespace, and `ensure_ascii=False`
    keeps a non-Latin instruction hashing as its own characters rather than as escape sequences.
    """
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _HASH_PREFIX + hashlib.sha256(encoded).hexdigest()


def seal_brief(
    request: RawLessonRequest,
    *,
    guard: GuardPort,
    sealed_at: datetime,
    template_version: str = TEMPLATE_VERSION,
) -> LessonBrief:
    """Sanitise, guard, hash, freeze.

    Order matters: the guard runs on cleaned text so a rule set sees one spelling, and the hash
    runs on the cleaned text so the identity matches what a worker will actually be handed.
    """
    instruction = sanitise_instruction(request.instruction)
    context = sanitise_context(request.context_items())

    verdict = guard.inspect(instruction, context)
    if verdict.decision is GuardDecision.DENY:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            {"field": "instruction", "reason": "guard_denied", "guard": verdict.guard_version},
        )

    plain_context = tuple(ContextItem(kind=item.kind, text=item.text.text) for item in context)
    payload = canonical_brief_payload(
        request,
        instruction.text,
        plain_context,
        template_version,
    )

    return LessonBrief(
        brief_id=derive_brief_id(request.job_id),
        job_id=request.job_id,
        subject=DEFAULT_SUBJECT,
        concept_id=None,
        instruction=instruction,
        context=context,
        constraints=request.domain_constraints(),
        profile=request.profile,
        guard=verdict,
        template_version=template_version,
        brief_hash=brief_hash_of(payload),
        sealed_at=sealed_at,
    )


def guard_verdict_from_document(document: Mapping[str, object]) -> GuardVerdict:
    """Read back what `guard_verdict_document` wrote.

    Tolerant on purpose. The column is jsonb precisely because its shape moves per rule set
    (D072), so a verdict written by an older guard must still load rather than crash a run six
    months later. What is missing is read as "nothing matched", which is what the demo's
    permissive guard says anyway; `guard_version` is the field that tells the two apart.
    """
    matched = document.get("matched_rules")
    return GuardVerdict(
        decision=GuardDecision(str(document.get("decision", GuardDecision.ALLOW.value))),
        risk=int(str(document.get("risk", 0))),
        matched_rules=tuple(str(rule) for rule in matched) if isinstance(matched, list) else (),
        guard_version=str(document.get("guard_version", "unknown")),
    )


def brief_from_record(record: BriefRecord, *, profile: ProfileId) -> LessonBrief:
    """The inverse of `brief_record`: a stored row back into the sealed brief it came from.

    Needed because the seam between orchestration and generation carries the row (a `BriefRecord`
    is what `BriefWriter.seal` returns and what `GenerationGateway.generate` takes) while the
    renderer works from the sealed value. Only intake may do this, for the same reason only
    intake may produce a `SanitisedText`.

    The text goes back through `intake.sanitiser` rather than past its private mint. That is not
    a formality: the sanitiser is idempotent on text it has already cleaned -- NFKC is idempotent,
    there are no control characters left to drop, whitespace is already collapsed, and the length
    is already under the cap -- so the string is unchanged and the type is honestly obtained.
    `original_length` and `truncated` are re-derived and therefore describe the stored text rather
    than what the learner first typed; nothing downstream reads either, and `brief_hash` is taken
    from the row rather than recomputed, so the identity of the brief cannot move here.

    `profile` is an argument because `briefs` has no profile column: it lives on `jobs`, which is
    the row that carries what was asked for.
    """
    return LessonBrief(
        brief_id=record.brief_id,
        job_id=record.job_id,
        subject=record.subject,
        concept_id=record.concept_id,
        instruction=sanitise_instruction(record.instruction),
        context=sanitise_context(record.context_items),
        constraints=record.constraints,
        profile=profile,
        guard=guard_verdict_from_document(record.guard_verdict),
        template_version=record.template_version,
        brief_hash=record.brief_hash,
        sealed_at=record.sealed_at,
    )


def brief_record(brief: LessonBrief) -> BriefRecord:
    """The `briefs` row for a sealed brief. Sole writer of that table is intake (D066).

    Unwraps `SanitisedText` for its column: the guarantee lives in the type system inside the
    process, and the database column is text either way.
    """
    return BriefRecord(
        brief_id=brief.brief_id,
        job_id=brief.job_id,
        brief_hash=brief.brief_hash,
        template_version=brief.template_version,
        subject=brief.subject,
        concept_id=brief.concept_id,
        instruction=brief.instruction.text,
        context_items=tuple(
            ContextItem(kind=item.kind, text=item.text.text) for item in brief.context
        ),
        constraints=brief.constraints,
        guard_verdict=guard_verdict_document(brief.guard),
        sealed_at=brief.sealed_at,
    )
