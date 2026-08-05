"""The sealed middle: cleaned text, the guard's verdict, the brief, and the worker's files.

Everything on this page sits between the two untrusted edges. A `RawLessonRequest` arrives as
pydantic at the HTTP edge and a `GenerationOutcome` comes back as pydantic from the worker; in
between the values are frozen dataclasses, because by then they have been through a boundary
and the property worth holding is that nobody can edit them afterwards.

`SanitisedText` uses the same mint trick as `AccessScope` (`plan/12-data-control.md`): it cannot
be constructed outside `intake.sanitiser`, so every downstream signature that takes one is a
compile-time statement that the cleaning step ran. Skipping the sanitiser stops being an
oversight and becomes a type error.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.domain.enums import ContextKind, ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import BriefId, JobId
from app.domain.records import JobConstraints

_MINT_TOKEN: Final[object] = object()
"""Module-private. Holding it is the proof that the sanitiser produced this string."""


@dataclass(frozen=True, slots=True)
class SanitisedText:
    """User text that has been through `intake.sanitiser`, and nothing else.

    `original_length` survives the trip so truncation stays visible: a 900-character question
    cut to 500 is a fact the brief records rather than a difference nobody can see. `truncated`
    is stored rather than derived so a cleaner that shortens text for another reason (control
    characters, collapsed whitespace) is not mistaken for a cap being hit.
    """

    text: str
    original_length: int
    truncated: bool
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _MINT_TOKEN:
            raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "text", "reason": "unsanitised"})
        if self.original_length < 0:
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                {"field": "original_length", "reason": "negative"},
            )
        # No `original_length >= len(text)` rule on purpose: NFKC expands as well as contracts
        # ("..." from one ellipsis, "fi" from one ligature), so a cleaned string is legitimately
        # longer than what arrived. The length is a record of the input, not a bound on output.

    @classmethod
    def _mint(cls, text: str, *, original_length: int, truncated: bool) -> SanitisedText:
        """The only constructor. Called by `app.intake.sanitiser` and by nothing else.

        Private by name because the guarantee it carries is "this string was normalised, capped
        and stripped", and a caller reaching past the underscore is asserting a cleaning pass it
        did not run.
        """
        return cls(
            text=text,
            original_length=original_length,
            truncated=truncated,
            _token=_MINT_TOKEN,
        )

    def __str__(self) -> str:
        return self.text

    def __len__(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class SanitisedContextItem:
    """One labelled context item, cleaned. `kind` is a closed enum and needs no cleaning."""

    kind: ContextKind
    text: SanitisedText


class GuardDecision(StrEnum):
    """What the prompt guard concluded (`plan/06-trust-boundary.md`, layer 3)."""

    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """The guard's answer, recorded on the brief whether or not any rule ran.

    `guard_version` is on the record for the same reason `validator_version` is on an artifact
    (D064): a verdict is only readable next to the rule set that produced it.
    """

    decision: GuardDecision
    risk: int
    matched_rules: tuple[str, ...]
    guard_version: str


def guard_verdict_document(verdict: GuardVerdict) -> Mapping[str, object]:
    """Render a verdict for the `briefs.guard_verdict` jsonb column.

    Pure containers, no serialisation library. The column is jsonb precisely because this shape
    changes per rule set (D072), and nothing queries inside it.
    """
    return {
        "decision": verdict.decision.value,
        "risk": verdict.risk,
        "matched_rules": list(verdict.matched_rules),
        "guard_version": verdict.guard_version,
    }


@dataclass(frozen=True, slots=True)
class LessonBrief:
    """The sealed intermediary product: what we asked for, not what the user typed.

    From here the exact bytes that reach a worker are known and reproducible. `brief_hash`
    covers the sealed content together with `template_version` (D061), so a change to prompt
    wording moves the hash and a quality regression can be bisected by re-rendering an old brief
    under a new template.

    The in-process shape of a `briefs` row; `BriefRecord` in `domain/records.py` is the row
    itself, with the text already unwrapped for the column.
    """

    brief_id: BriefId
    job_id: JobId
    subject: str
    concept_id: str | None
    instruction: SanitisedText
    context: tuple[SanitisedContextItem, ...]
    constraints: JobConstraints
    profile: ProfileId
    guard: GuardVerdict
    template_version: str
    brief_hash: str
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class BundleFile:
    """One file in the worker's workspace. Text only; the bundle carries no binary."""

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class BriefBundle:
    """The complete file set handed to a worker (D029).

    Files, never messages. User text is delivered as fenced data inside documents we author, and
    the agent's instructions live in the pinned template repo, so nothing a learner wrote ever
    occupies an instruction position.
    """

    brief_id: BriefId
    template_version: str
    brief_hash: str
    files: tuple[BundleFile, ...]

    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)

    def file(self, path: str) -> BundleFile:
        for item in self.files:
            if item.path == path:
                return item
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": "path", "reason": "not_in_bundle"})
