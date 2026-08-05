"""Step 3 of 4: claimed files become stored, verified artifact rows.

Stage `COLLECTING`, 80 percent. `VERIFYING` (90) has no step of its own in the demo because
verification happens inside custody, on the same bytes it stored, and a step boundary there would
write a percent nobody had earned.
"""

from app.domain.records import JobRecord
from app.orchestration.ports import ArtifactWriter, GenerationOutcome, HarvestOutcome


async def run_harvest(
    artifacts: ArtifactWriter, *, job: JobRecord, outcome: GenerationOutcome
) -> HarvestOutcome:
    """Hand custody the claims and get back the rows it wrote.

    Custody is given `principal_id` and `chat_context_id` rather than left to look them up,
    because it copies both onto every artifact row at insert (A6/D088, D073) so that listing a
    learner's artifacts stays one index scan. It is not given the `JobRecord`: it has no business
    reading a job status, and giving it one is the first step towards writing one.

    Everything that makes this safe is on the other side of the port: the path allowlist, the
    size cap, the media probe and the validator chain. This step's whole contribution is that the
    bytes are fetched by us from a place we chose, rather than accepted from a manifest.
    """
    return await artifacts.harvest(
        job_id=job.job_id,
        principal_id=job.principal_id,
        chat_context_id=job.chat_context_id,
        profile=job.profile,
        outcome=outcome,
    )
