"""Intake's seam: what crosses into this package, and the two ports it calls out through.

`RawLessonRequest` is the step boundary in the sense of D063. Orchestration hands intake a
payload; intake re-validates it with pydantic before any step logic touches it, so a queue
message, a resumed run, or a future HTTP-transported step all meet the same gate. `extra=forbid`
means a payload carrying `system_prompt`, `output_paths`, or `template_override` is rejected
rather than partially honoured.

`GuardPort` and `TemplateSource` are `typing.Protocol`: structural, so an adapter satisfies one
by having the methods rather than by importing this module.
"""

from typing import Annotated, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.brief import GuardDecision, GuardVerdict, SanitisedContextItem, SanitisedText
from app.domain.enums import ContextKind, ProfileId, ReadingLevel
from app.domain.ids import JOB_ID_PATTERN, JobId
from app.domain.records import ContextItem, JobConstraints, SubmitJobCommand

MAX_CONTEXT_ITEMS: Final[int] = 8
"""`CreateJobRequest.context` is capped at eight items (`docs/demo.md`)."""

_STRICT: ConfigDict = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
"""Inbound rules for every model on this page: unknown fields are rejected, records are frozen."""


class RawContextItem(BaseModel):
    """One untrusted context item as it reaches a step."""

    model_config = _STRICT

    kind: ContextKind
    text: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class RawConstraints(BaseModel):
    """The effective constraints, revalidated. Out of range is rejected, never clamped (D080)."""

    model_config = _STRICT

    max_duration_s: Annotated[int, Field(ge=15, le=180)]
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")]
    reading_level: ReadingLevel | None = None


class RawLessonRequest(BaseModel):
    """What intake is asked to seal. Untrusted, and validated as such (D063).

    Carries `job_id` because the brief id derives from it and because a step payload that cannot
    name its job cannot be resumed. It carries no principal: whose job this is lives on the row
    and never reaches the text-handling code (`plan/12-data-control.md`).
    """

    model_config = _STRICT

    job_id: Annotated[str, StringConstraints(pattern=JOB_ID_PATTERN)]
    instruction: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    context: Annotated[tuple[RawContextItem, ...], Field(max_length=MAX_CONTEXT_ITEMS)] = ()
    constraints: RawConstraints
    profile: ProfileId = ProfileId.VIDEO_SHORT_V1

    @classmethod
    def from_command(cls, job_id: JobId, command: SubmitJobCommand) -> RawLessonRequest:
        """Build the payload from the command the API already validated.

        Revalidating a value that has been through the HTTP edge looks redundant and is not:
        this is the shape a queued step reads back, and the only way the two can be guaranteed
        identical is for both to go through the same model.
        """
        return cls.model_validate(
            {
                "job_id": job_id,
                "instruction": command.instruction,
                "context": [
                    {"kind": item.kind.value, "text": item.text} for item in command.context
                ],
                "constraints": {
                    "max_duration_s": command.constraints.max_duration_s,
                    "language": command.constraints.language,
                    "reading_level": (
                        command.constraints.reading_level.value
                        if command.constraints.reading_level is not None
                        else None
                    ),
                },
                "profile": command.profile.value,
            }
        )

    def context_items(self) -> tuple[ContextItem, ...]:
        """The domain shape, still uncleaned. `intake.sanitiser` is the next stop."""
        return tuple(ContextItem(kind=item.kind, text=item.text) for item in self.context)

    def domain_constraints(self) -> JobConstraints:
        return JobConstraints(
            max_duration_s=self.constraints.max_duration_s,
            language=self.constraints.language,
            reading_level=self.constraints.reading_level,
        )


@runtime_checkable
class GuardPort(Protocol):
    """Layer 3 on the way in: does this text look like an attack (`plan/06-trust-boundary.md`).

    Runs after the sanitiser, so a rule set sees one normalised spelling rather than every
    fullwidth and zero-width variant of the same phrase.
    """

    version: str

    def inspect(
        self,
        instruction: SanitisedText,
        context: tuple[SanitisedContextItem, ...],
    ) -> GuardVerdict: ...


class PermissiveGuard:
    """The demo's guard: it admits everything and says so on the record.

    @audit NO PROMPT GUARD. `docs/demo.md` cuts the guard, the intent classifier, and the
    concept registry from the build, so nothing inspects a learner's text for instruction
    override, role-play framing, or requests for the template contents. The verdict is still
    written to `briefs.guard_verdict` with `guard_version="permissive.v0"`, so a brief sealed
    today is distinguishable from one sealed under real rules rather than looking identically
    approved.

    @TODO rule set behind this port, plus its fixture corpus
    (`docs/plan/06-trust-boundary.md`, stage 7 of `docs/plan/13-mvp.md`). The layers that follow
    already assume this one is imperfect; right now they have to assume it is absent.
    """

    version: str = "permissive.v0"

    def inspect(
        self,
        instruction: SanitisedText,
        context: tuple[SanitisedContextItem, ...],
    ) -> GuardVerdict:
        return GuardVerdict(
            decision=GuardDecision.ALLOW,
            risk=0,
            matched_rules=(),
            guard_version=self.version,
        )


@runtime_checkable
class TemplateSource(Protocol):
    """Where a brief template comes from (D061).

    The demo reads a versioned directory in this package. The eventual source is a git repo
    pinned by commit hash, which is why `template_version` is an argument rather than a path
    the caller assembles.
    """

    def read(self, template_version: str, name: str) -> str: ...
