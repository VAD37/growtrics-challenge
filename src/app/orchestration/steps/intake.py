"""Step 1 of 4: the untrusted body becomes a sealed brief.

Stage `PREPARING`, 25 percent. Imports `app.domain` and `app.orchestration.ports` and nothing
else; no step imports another (D065).
"""

from app.domain.records import BriefRecord, JobRecord, StoredRequest
from app.orchestration.ports import BriefWriter


async def run_intake(briefs: BriefWriter, *, job: JobRecord, request: StoredRequest) -> BriefRecord:
    """Hand the stored body to intake and get back the row it sealed.

    The step is this thin on purpose. Sanitising, the guard verdict, the concept classifier and
    the template render are all intake's, behind `BriefWriter.seal`, and orchestration's only
    business here is which job the brief belongs to and under which constraints.

    @TODO the guard and the classifier are cut from the demo (docs/demo.md). When they land they
    land inside `app.intake`, and a rejected brief arrives here as a `DomainError` that
    `engine/policy.py` classifies like any other step failure. Nothing in this module changes.
    """
    return await briefs.seal(
        job_id=job.job_id,
        request=request,
        constraints=job.constraints,
        profile=job.profile,
    )
