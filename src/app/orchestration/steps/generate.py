"""Step 2 of 4: the sealed brief goes out to the agent, claims come back.

Stage `GENERATING`, 60 percent. The one step that talks to something outside this process, and
the only one whose return value is untrusted.
"""

from app.domain.ids import SessionId, TraceId
from app.domain.records import BriefRecord, JobRecord
from app.orchestration.ports import GenerationGateway, GenerationOutcome


async def run_generate(
    generation: GenerationGateway,
    *,
    job: JobRecord,
    brief: BriefRecord,
    session_id: SessionId,
    trace_id: TraceId,
) -> GenerationOutcome:
    """Run one generation attempt and return what the worker claims it made.

    `session_id` and `trace_id` are the only identities that cross (`plan/12-data-control.md`).
    Both are `uuid5` off `(job_id, attempt)`, which is one-way, so a worker holding either cannot
    address the job, the principal or the chat context, and there is nothing else in the payload
    to address them with.

    The demo's backend is a mock that returns a descriptor for a committed sample video (scope
    override item 6). That is a backend swap and not a bypass: this step, the ACL behind it and
    the harvest that follows all run exactly as they would against a real agent, and the
    descriptors are treated as claims either way.

    @TODO a real backend needs placement, polling and a timeout on the session
    (docs/plan/07-distribution.md). None of that changes this signature: the wait lives behind
    the gateway, and this step still gets one outcome or one exception.
    """
    return await generation.generate(
        session_id=session_id,
        trace_id=trace_id,
        brief=brief,
        constraints=job.constraints,
        profile=job.profile,
    )
