"""Intake from the outside: one method, and the only one orchestration is allowed to call.

`orchestration.ports.BriefWriter` declares the shape and `SealBrief` satisfies it structurally,
so neither package imports the other (`plan/03-module-layout.md`, rule 5). Everything inside --
which cleaner ran, what the guard concluded, which template pack was used -- is invisible from
the other side, which is the whole point of the seam being one method wide.

The order is fixed and it is the trust boundary: revalidate, sanitise, guard, seal, insert. The
revalidation is not ceremony. The body arrives here out of the `requests` table rather than out
of a request handler, so it has been through the HTTP edge's models once and through a jsonb
column since; `RawLessonRequest` is what makes the shape a step reads back identical to the
shape the edge accepted (D063).

The constraints come from the `jobs` row and never from the stored body. `jobs.constraints` is
the effective set after the profile's caps were applied (D080), and re-reading the caller's
numbers here would seal a brief against what was asked for rather than against what was agreed.
"""

from datetime import datetime
from typing import Final, Protocol

from app.domain.enums import ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import JobId
from app.domain.records import BriefRecord, JobConstraints, StoredRequest
from app.intake.ports import BriefRepository, GuardPort, RawLessonRequest
from app.intake.sealer import TEMPLATE_VERSION, brief_record, seal_brief

__all__ = ["SealBrief"]


class ReadsTheClock(Protocol):
    """`orchestration.ports.Clock`, structurally.

    Restated rather than imported: intake may not point at orchestration (rule 5), and the whole
    of what sealing needs from time is one method. The composition root passes the same object
    the runner and the sweeper hold, so a frozen clock in a test freezes `sealed_at` too.
    """

    def now(self) -> datetime: ...


class SealBrief:
    """`orchestration.ports.BriefWriter`: untrusted body in, sealed `briefs` row out.

    Sole writer of `briefs` (D066), through the repository it is handed.
    """

    def __init__(
        self,
        briefs: BriefRepository,
        *,
        guard: GuardPort,
        clock: ReadsTheClock,
        template_version: str = TEMPLATE_VERSION,
    ) -> None:
        self._briefs: Final[BriefRepository] = briefs
        self._guard: Final[GuardPort] = guard
        self._clock: Final[ReadsTheClock] = clock
        self._template_version: Final[str] = template_version

    async def seal(
        self,
        *,
        job_id: JobId,
        request: StoredRequest,
        constraints: JobConstraints,
        profile: ProfileId,
    ) -> BriefRecord:
        """Sanitise the stored body, seal the brief, insert the row, return it."""
        raw = _revalidate(job_id, request, constraints, profile)
        brief = seal_brief(
            raw,
            guard=self._guard,
            sealed_at=self._clock.now(),
            template_version=self._template_version,
        )
        return await self._briefs.insert(brief_record(brief))


def _revalidate(
    job_id: JobId,
    request: StoredRequest,
    constraints: JobConstraints,
    profile: ProfileId,
) -> RawLessonRequest:
    """The stored body as a step payload, or `INVALID_REQUEST` naming the field.

    A `ValidationError` is converted rather than allowed to escape, because pydantic's message
    quotes the input it rejected and that input is a learner's text on its way into a log. The
    conversion also means a broken payload reaches `engine/policy.classify_failure` as a
    `DomainError` like every other step failure rather than as a driver-shaped exception.
    """
    body = request.raw
    payload = {
        "job_id": job_id,
        "instruction": body.get("instruction"),
        "context": body.get("context", ()),
        "constraints": {
            "max_duration_s": constraints.max_duration_s,
            "language": constraints.language,
            "reading_level": (
                constraints.reading_level.value if constraints.reading_level is not None else None
            ),
        },
        "profile": profile.value,
    }
    try:
        return RawLessonRequest.model_validate(payload)
    except Exception as error:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            {"field": "body", "reason": "stored_request_does_not_revalidate"},
        ) from error
