"""What a client may send, and the pure mapping to what orchestration receives.

`extra="forbid"` on every inbound model. An unknown field is a `400`, not a shrug: a client
that sends `principal_id`, `output_paths`, or `system_prompt` finds out immediately that it
does not get to. Out of range is rejected and never clamped (D080) -- a silently altered
request is a silent partial success, which N5 rules out.

Deliberately absent from the request, all four for the same reason -- the client is untrusted
input, not a co-author of the job:

* no principal. It comes from the auth stub; a caller cannot name whose job this is.
* no paths, mime types, or check names. The client picks a `profile` and the server resolves
  it. A client-authored output contract lets the caller tell the validator what to accept.
* no prompt, system message, or template override. User text is data, never instruction (D029).
* no `Idempotency-Key`. It is gone rather than optional (scope override item 2, supersedes
  D055 and D089): not read, not validated, not a field. Two identical submits are two jobs.
"""

from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.domain.enums import ContextKind, ProfileId, ReadingLevel
from app.domain.ids import ChatContextId
from app.domain.records import ContextItem, JobConstraints, SubmitJobCommand

INSTRUCTION_MAX_CHARS: Final[int] = 500
CONTEXT_MAX_ITEMS: Final[int] = 8
DURATION_MIN_SECONDS: Final[int] = 15
DURATION_MAX_SECONDS: Final[int] = 180
LANGUAGE_TAG_PATTERN: Final[str] = r"^[a-z]{2}(-[A-Z]{2})?$"

BUILT_PROFILES: Final[frozenset[ProfileId]] = frozenset({ProfileId.VIDEO_SHORT_V1})
"""Which members of the frozen `ProfileId` vocabulary this build can actually make.

`html.lesson.v1` is designed and not built (D081): agent-authored HTML is active content and
the isolated serving origin it requires is a deployment fact nobody has provided. Asking for
it is rejected at the edge rather than accepted and failed four stages later.

@TODO `plan/14-api-schema.md` gives this its own code, `OUTPUT_PROFILE_NOT_SUPPORTED` (422).
That member is not in `ErrorCode` yet, and adding one needs a decision line, so the rejection
is a `400 INVALID_REQUEST` naming the profile until the stage that builds a second profile.
"""


class ContextItemIn(BaseModel):
    """One labelled piece of context. The kinds are a closed set for the same reason the
    instruction is capped: an open vocabulary is a free-text field wearing a label."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    kind: ContextKind
    text: Annotated[str, StringConstraints(min_length=1, max_length=INSTRUCTION_MAX_CHARS)]

    def to_domain(self) -> ContextItem:
        return ContextItem(kind=self.kind, text=self.text)


class JobOptionsIn(BaseModel):
    """What to make and under what constraints. Every field has a server-owned default."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    profile: ProfileId = ProfileId.VIDEO_SHORT_V1
    max_duration_s: Annotated[int, Field(ge=DURATION_MIN_SECONDS, le=DURATION_MAX_SECONDS)] = 90
    language: Annotated[str, StringConstraints(pattern=LANGUAGE_TAG_PATTERN)] = "en"
    reading_level: ReadingLevel | None = None

    @field_validator("profile")
    @classmethod
    def _must_be_built(cls, value: ProfileId) -> ProfileId:
        if value not in BUILT_PROFILES:
            raise ValueError(f"profile is not built in this deployment: {value.value}")
        return value


class CreateJobRequest(BaseModel):
    """`POST /v1/jobs`. The body from `docs/demo.md`, minus the idempotency header it dropped."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    instruction: Annotated[str, StringConstraints(min_length=1, max_length=INSTRUCTION_MAX_CHARS)]
    context: Annotated[list[ContextItemIn], Field(max_length=CONTEXT_MAX_ITEMS)] = Field(
        default_factory=list
    )
    options: JobOptionsIn = Field(default_factory=JobOptionsIn)
    chat_context_id: ChatContextId | None = None
    """Optional everywhere: on the request, on `jobs`, and on `artifacts` (D087, A5).

    Foreign. We index it and do not mint it, so the pattern is all the validation there is.
    """


def to_command(request: CreateJobRequest) -> SubmitJobCommand:
    """The one mapping from the wire to the domain. Pure, and the router's only translation.

    The constraints travel as requested rather than as effective: `video.short.v1` caps
    duration tighter than the field permits, and applying a profile cap is orchestration's
    decision, echoed back on `ConstraintsView`. Doing it here would put a rule in the edge and
    give the caller two different answers depending on which stage read the value.
    """
    return SubmitJobCommand(
        instruction=request.instruction,
        context=tuple(item.to_domain() for item in request.context),
        constraints=JobConstraints(
            max_duration_s=request.options.max_duration_s,
            language=request.options.language,
            reading_level=request.options.reading_level,
        ),
        profile=request.options.profile,
        chat_context_id=request.chat_context_id,
    )
