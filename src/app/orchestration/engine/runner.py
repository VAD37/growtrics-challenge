"""One job, four stages, one writer of status.

The runner is the only thing in the system that calls `JobRepository.apply_transition`, which is
the only method that moves a `jobs` row (D066, `plan/12-data-control.md`). Everything else it
does is call one step and hand its typed output to the next.

Progress comes from the static stage map (D092) and never from a step's opinion, so the bar is
monotonic because `DEMO_STAGE_ORDER` is (D042). `VERIFYING` has no step of its own in the demo:
verification happens inside custody's harvest, and inventing a step boundary for it would mean
writing a percent nobody had earned. The stages the runner writes are therefore 25, 60, 80, 95,
and then 100 on the terminal transition.

Failure path, per docs/demo.md D2: the failure is populated from `ERROR_CATALOG`, the status
becomes `FAILED`, and the work item is not retried. The stage becomes `StageName.FAILED`, which
has no percent, so the progress that was already reached is carried across unchanged rather than
zeroed.
"""

import logging
from typing import Final

from app.domain.enums import TERMINAL_JOB_STATUSES, JobStatus, StageName, percent_for
from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode
from app.domain.ids import ArtifactId, BriefId, TraceId, derive_session_id, derive_trace_id
from app.domain.records import ClaimedWorkItem, FailureRecord, JobRecord
from app.orchestration.engine.policy import classify_failure
from app.orchestration.ports import (
    ArtifactWriter,
    BriefWriter,
    Clock,
    GenerationGateway,
    JobRepository,
    JobTransition,
    RequestStore,
)
from app.orchestration.steps.generate import run_generate
from app.orchestration.steps.harvest import run_harvest
from app.orchestration.steps.intake import run_intake
from app.orchestration.steps.publish import run_publish

logger = logging.getLogger(__name__)

RUN_STAGES: Final[tuple[StageName, ...]] = (
    StageName.PREPARING,
    StageName.GENERATING,
    StageName.COLLECTING,
    StageName.PUBLISHING,
)
"""The stage each of the four steps occupies, in order. A subset of `DEMO_STAGE_ORDER`."""


class JobRunner:
    """Executes one claimed work item to a terminal status.

    It never raises for a job that failed: a failed lesson is a job outcome, not a worker error,
    and the loop that called this has to keep going. It returns `None` only when the work item
    points at a job that is not there, which at-least-once delivery makes possible.
    """

    def __init__(
        self,
        *,
        jobs: JobRepository,
        requests: RequestStore,
        briefs: BriefWriter,
        generation: GenerationGateway,
        artifacts: ArtifactWriter,
        clock: Clock,
    ) -> None:
        self._jobs: JobRepository = jobs
        self._requests: RequestStore = requests
        self._briefs: BriefWriter = briefs
        self._generation: GenerationGateway = generation
        self._artifacts: ArtifactWriter = artifacts
        self._clock: Clock = clock

    async def run(self, item: ClaimedWorkItem) -> JobRecord | None:
        job = await self._jobs.load_for_run(item.job_id)
        if job is None:
            logger.warning("work item %s points at no job; dropping it", item.item_id)
            return None
        if job.status in TERMINAL_JOB_STATUSES:
            # Invariant 4 of `VideoJob`: nothing leaves a terminal status, not even a
            # redelivery of the same queue row.
            logger.info("job %s is already %s; nothing to run", job.job_id, job.status)
            return job

        trace_id = derive_trace_id(job.job_id, job.attempt)
        session_id = derive_session_id(job.job_id, job.attempt)
        stage = RUN_STAGES[0]

        try:
            job = await self._enter(job, StageName.PREPARING)
            stage = StageName.PREPARING
            request = await self._requests.load(job.request_key)
            if request is None:
                # The `requests` row is written in the same transaction as the job, so its
                # absence is a broken invariant rather than a race. Fail the job loudly.
                raise DomainError(
                    ErrorCode.GENERATION_FAILED, {"reason": "stored request is missing"}
                )
            brief = await run_intake(self._briefs, job=job, request=request)

            job = await self._enter(job, StageName.GENERATING, brief_id=brief.brief_id)
            stage = StageName.GENERATING
            outcome = await run_generate(
                self._generation,
                job=job,
                brief=brief,
                session_id=session_id,
                trace_id=trace_id,
            )

            job = await self._enter(job, StageName.COLLECTING)
            stage = StageName.COLLECTING
            harvested = await run_harvest(self._artifacts, job=job, outcome=outcome)

            job = await self._enter(job, StageName.PUBLISHING)
            stage = StageName.PUBLISHING
            primary = await run_publish(self._artifacts, primary=harvested.primary)

            return await self._succeed(job, primary.artifact_id)
        except Exception as error:
            logger.exception("job %s failed at %s", job.job_id, stage)
            return await self._fail(job, stage=stage, trace_id=trace_id, error=error)

    async def _enter(
        self, job: JobRecord, stage: StageName, *, brief_id: BriefId | None = None
    ) -> JobRecord:
        """Move the job to a stage before the step that occupies it runs.

        Before rather than after, so a job that dies inside a step is last seen at the stage that
        killed it rather than at the one it survived.
        """
        return await self._jobs.apply_transition(
            JobTransition(
                job_id=job.job_id,
                expected_version=job.version,
                status=JobStatus.RUNNING,
                stage=stage,
                progress_percent=percent_for(stage),
                brief_id=brief_id,
            )
        )

    async def _succeed(self, job: JobRecord, artifact_id: ArtifactId) -> JobRecord:
        return await self._jobs.apply_transition(
            JobTransition(
                job_id=job.job_id,
                expected_version=job.version,
                status=JobStatus.SUCCEEDED,
                stage=StageName.DONE,
                progress_percent=percent_for(StageName.DONE),
                artifact_id=artifact_id,
            )
        )

    async def _fail(
        self,
        job: JobRecord,
        *,
        stage: StageName,
        trace_id: TraceId,
        error: BaseException,
    ) -> JobRecord:
        verdict = classify_failure(error)
        failure = FailureRecord(
            code=verdict.code,
            stage=stage,
            message=ERROR_CATALOG[verdict.code].message,
            retryable=verdict.retryable,
            occurred_at=self._clock.now(),
            trace_id=trace_id,
        )
        return await self._jobs.apply_transition(
            JobTransition(
                job_id=job.job_id,
                expected_version=job.version,
                status=JobStatus.FAILED,
                stage=StageName.FAILED,
                # `StageName.FAILED` has no percent and the bar never goes backwards (D042), so
                # the job keeps whatever it had reached.
                progress_percent=job.progress_percent,
                failure=failure,
            )
        )
