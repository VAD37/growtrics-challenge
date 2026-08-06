"""Generation from the outside: a sealed brief goes out, a worker's document comes back.

`orchestration.ports.GenerationGateway` declares the shape and `RunGeneration` satisfies it
structurally, so neither package imports the other. What is behind it is a `GenerationBackend`
and nothing else: the mock in `backends/mock.py` today, a rented machine later, and the swap
costs a constructor argument (D101).

Three things this class does and one it deliberately does not.

It renders. The seam carries a `BriefRecord`, and a worker is handed files (D029), so the row is
turned into a `BriefBundle` on the way out. The renderer is intake's and arrives as an argument
rather than as an import, because `app.generation` may not reach into another context: the
composition root passes `intake.rendering.bundle_for_record` and a test passes whatever it likes.

It resolves the output contract, from the profile and from nowhere else. The same object is
rendered into the worker's `OUTPUT_CONTRACT.json` and read back by `custody.verifier` as the
acceptance test (D077), so resolving it twice from one profile is what keeps those two the same
value rather than two lookups that agree today.

It carries the raw answer across unopened. `generation.acl` is the only module that turns a
worker's document into domain types, and it runs inside `custody.harvester` where the bytes are
pulled, so accepting a manifest and fetching the files it names is one decision. Parsing here as
well would be a second definition of what a worker may claim.

What it does not do is wait cleverly. `generate` is one call that returns when the backend is
done. @TODO placement, polling and a wall-clock timeout for a real backend
(`docs/plan/07-generation.md`, `plan/15-engine-seam.md`); none of it changes this signature,
because the waiting lives behind the port.
"""

from collections.abc import Callable
from typing import Final

from app.domain.artifact import GenerationOutcome
from app.domain.brief import BriefBundle
from app.domain.contracts import OutputContract, contract_for
from app.domain.enums import ProfileId
from app.domain.ids import SessionId, TraceId
from app.domain.records import BriefRecord, JobConstraints
from app.generation.ports import DEFAULT_LIMITS, ExecutionLimits, GenerationBackend
from app.generation.ports import GenerationRequest as WorkerRequest

__all__ = ["BundleRenderer", "RunGeneration"]

type BundleRenderer = Callable[[BriefRecord, OutputContract, ProfileId], BriefBundle]
"""How a stored brief becomes the files a worker receives.

A callable rather than a protocol because it is one pure function with no state, and an argument
rather than an import because the function belongs to `app.intake` and this package may not
reach for it (`plan/03-module-layout.md`, rule 5).
"""


class RunGeneration:
    """`orchestration.ports.GenerationGateway` over one `GenerationBackend`.

    Writes nothing, reads no table, and never sees a job. `session_id` and `trace_id` are the
    only identities that cross to a worker (`plan/12-data-control.md`); both are `uuid5` off
    `(job_id, attempt)` and neither can be inverted.
    """

    def __init__(
        self,
        backend: GenerationBackend,
        *,
        render: BundleRenderer,
        limits: ExecutionLimits = DEFAULT_LIMITS,
    ) -> None:
        self._backend: Final[GenerationBackend] = backend
        self._render: Final[BundleRenderer] = render
        self._limits: Final[ExecutionLimits] = limits

    async def generate(
        self,
        *,
        session_id: SessionId,
        trace_id: TraceId,
        brief: BriefRecord,
        constraints: JobConstraints,
        profile: ProfileId,
    ) -> GenerationOutcome:
        """Run one generation attempt and hand back what the worker claims, unopened.

        `constraints` reaches the worker only through the rendered `CONSTRAINTS.md`, which is
        already built from the brief's own copy of them. The argument stays on the port because
        the effective constraints live on `jobs` and a future backend may need to enforce them
        rather than merely state them.
        """
        del constraints
        contract = contract_for(profile)
        bundle = self._render(brief, contract, profile)
        document = await self._backend.generate(
            WorkerRequest(
                session_id=session_id,
                trace_id=trace_id,
                bundle=bundle,
                contract=contract,
                limits=self._limits,
            )
        )
        return GenerationOutcome(session_id=session_id, document=document)
