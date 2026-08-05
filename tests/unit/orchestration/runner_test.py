"""One job through the four stages.

The run is the only place job status is written, so the assertions are mostly about the shape of
the transition sequence rather than about any single step. Progress has to climb and never fall
(D042), the end has to be terminal with an artifact on it, and a failure has to leave a failure
row rather than a stuck `RUNNING`.
"""

import pytest
from fakes_test import (
    ARTIFACT_ID,
    JOB_ID,
    TRACE_ID,
    FakeArtifactWriter,
    FakeBriefWriter,
    FakeGenerationGateway,
    FakeJobRepository,
    FakeRequestStore,
    FrozenClock,
    make_claimed_item,
    make_job,
    make_request,
)

from app.domain.enums import DEMO_STAGE_ORDER, JobStatus, StageName, percent_for
from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode
from app.domain.ids import derive_session_id, derive_trace_id
from app.domain.records import JobRecord
from app.orchestration.engine.runner import RUN_STAGES, JobRunner


class Harness:
    """The runner with every port faked, so a test can reach in and break one of them."""

    def __init__(self) -> None:
        self.clock = FrozenClock()
        self.jobs = FakeJobRepository(clock=self.clock)
        self.requests = FakeRequestStore()
        self.briefs = FakeBriefWriter()
        self.generation = FakeGenerationGateway()
        self.artifacts = FakeArtifactWriter()
        self.jobs.seed(make_job())
        self.requests.seed(make_request())
        self.runner = JobRunner(
            jobs=self.jobs,
            requests=self.requests,
            briefs=self.briefs,
            generation=self.generation,
            artifacts=self.artifacts,
            clock=self.clock,
        )

    async def run(self) -> JobRecord | None:
        return await self.runner.run(make_claimed_item())


# --------------------------------------------------------------------------- the happy path


async def test_a_finished_run_is_succeeded_with_its_artifact() -> None:
    harness = Harness()
    job = await harness.run()

    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert job.stage is StageName.DONE
    assert job.progress_percent == 100
    assert job.artifact_id == ARTIFACT_ID
    assert job.brief_id is not None
    assert job.failure is None


async def test_the_run_walks_the_four_stages_in_order() -> None:
    harness = Harness()
    await harness.run()

    stages = [transition.stage for transition in harness.jobs.transitions]
    assert stages == [*RUN_STAGES, StageName.DONE]
    assert RUN_STAGES == (
        StageName.PREPARING,
        StageName.GENERATING,
        StageName.COLLECTING,
        StageName.PUBLISHING,
    )


async def test_progress_never_goes_backwards() -> None:
    """D042. The bar is monotonic because `DEMO_STAGE_ORDER` is, and this checks the whole run."""
    harness = Harness()
    started = harness.jobs.rows[JOB_ID].progress_percent
    await harness.run()

    percents = [started, *(t.progress_percent for t in harness.jobs.transitions)]
    assert percents == sorted(percents)
    assert len(set(percents)) == len(percents)
    assert percents == [10, 25, 60, 80, 95, 100]


async def test_every_written_percent_is_the_static_map() -> None:
    """Progress comes from the stage map, never from a step's opinion (D092)."""
    harness = Harness()
    await harness.run()

    for transition in harness.jobs.transitions:
        assert transition.progress_percent == percent_for(transition.stage)
        assert transition.stage in DEMO_STAGE_ORDER


async def test_the_run_is_running_until_it_is_not() -> None:
    harness = Harness()
    await harness.run()

    statuses = [transition.status for transition in harness.jobs.transitions]
    assert statuses == [JobStatus.RUNNING] * 4 + [JobStatus.SUCCEEDED]


async def test_each_step_is_called_once_with_the_ids_derived_off_the_job() -> None:
    harness = Harness()
    await harness.run()

    assert harness.briefs.calls == [JOB_ID]
    assert harness.generation.calls == [derive_session_id(JOB_ID, 0)]
    assert harness.artifacts.harvested == [JOB_ID]
    assert harness.artifacts.published == [ARTIFACT_ID]


async def test_every_transition_uses_the_version_it_read() -> None:
    """Optimistic concurrency: the fake asserts it, and this proves the runner threads it."""
    harness = Harness()
    await harness.run()
    versions = [transition.expected_version for transition in harness.jobs.transitions]
    assert versions == [0, 1, 2, 3, 4]


# --------------------------------------------------------------------------- the failure path


@pytest.mark.parametrize(
    ("port", "stage"),
    [
        ("briefs", StageName.PREPARING),
        ("generation", StageName.GENERATING),
        ("artifacts", StageName.COLLECTING),
    ],
)
async def test_a_broken_step_fails_the_job_at_the_stage_it_broke(
    port: str, stage: StageName
) -> None:
    harness = Harness()
    getattr(harness, port).raises = RuntimeError("the renderer fell over")

    job = await harness.run()

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.stage is StageName.FAILED
    assert job.failure is not None
    assert job.failure.stage is stage
    assert job.failure.code is ErrorCode.GENERATION_FAILED
    assert job.failure.retryable is False
    assert job.failure.message == ERROR_CATALOG[ErrorCode.GENERATION_FAILED].message
    assert job.failure.trace_id == derive_trace_id(JOB_ID, 0)


async def test_a_failed_job_keeps_the_progress_it_reached() -> None:
    """`StageName.FAILED` has no percent, and zeroing the bar would break D042."""
    harness = Harness()
    harness.artifacts.raises = RuntimeError("harvest refused the path")

    job = await harness.run()

    assert job is not None
    assert job.progress_percent == percent_for(StageName.COLLECTING)


async def test_a_failed_job_is_not_retried() -> None:
    """docs/demo.md D2: the work item is not retried, so the runner writes one terminal row."""
    harness = Harness()
    harness.generation.raises = RuntimeError("no worker")

    await harness.run()

    terminal = [t for t in harness.jobs.transitions if t.status is JobStatus.FAILED]
    assert len(terminal) == 1
    assert len(harness.generation.calls) == 1


async def test_a_missing_request_row_fails_the_job_rather_than_crashing_the_worker() -> None:
    harness = Harness()
    harness.requests.rows.clear()

    job = await harness.run()

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.failure is not None
    assert job.failure.stage is StageName.PREPARING


async def test_a_domain_failure_code_survives_classification() -> None:
    harness = Harness()
    harness.briefs.raises = DomainError(ErrorCode.GENERATION_FAILED, {"reason": "guard"})

    job = await harness.run()

    assert job is not None
    assert job.failure is not None
    assert job.failure.code is ErrorCode.GENERATION_FAILED


# --------------------------------------------------------------------------- redelivery


async def test_an_unknown_job_is_not_an_exception() -> None:
    """At-least-once delivery: a queue row can outlive its job. The worker moves on."""
    harness = Harness()
    harness.jobs.rows.clear()

    assert await harness.run() is None
    assert harness.jobs.transitions == []


async def test_a_terminal_job_is_left_alone() -> None:
    """Invariant 4 of `VideoJob`: nothing leaves a terminal status, not even a redelivery."""
    harness = Harness()
    harness.jobs.seed(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=7))

    job = await harness.run()

    assert job is not None
    assert job.status is JobStatus.SUCCEEDED
    assert harness.jobs.transitions == []
    assert harness.briefs.calls == []


async def test_the_trace_id_is_stable_for_the_attempt() -> None:
    harness = Harness()
    harness.briefs.raises = RuntimeError("boom")
    job = await harness.run()

    assert job is not None
    assert job.failure is not None
    assert job.failure.trace_id != TRACE_ID
    assert job.failure.trace_id == derive_trace_id(JOB_ID, 0)
