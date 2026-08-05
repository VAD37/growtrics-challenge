"""The five use cases behind the API seam.

Three things get held down here: the capacity gate refuses the fourth job and not the third,
one submit writes three rows or none, and every read admits any caller because the demo has no
authorisation (scope override item 1). The last one is a test of an absence on purpose: if
somebody restores the ownership predicate without saying so, this file goes red and names it.
"""

from collections.abc import Mapping
from typing import Final

import pytest
from fakes_test import (
    ARTIFACT_ID,
    JOB_ID,
    OTHER_PRINCIPAL,
    PRINCIPAL,
    REQUEST_KEY,
    T0,
    AdmittingAdmission,
    FakeArtifactReader,
    FakeJobRepository,
    FakeUnitOfWork,
    FrozenClock,
    RefusingAdmission,
    UnitOfWorkFailure,
    constraints,
    demo_scope,
    make_artifact,
    make_job,
)

from app.domain.enums import JobStatus, ProfileId, StageName
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import derive_job_id, derive_work_item_id
from app.domain.records import SubmitJobCommand
from app.orchestration.service import (
    ActiveJobLimit,
    ArtifactService,
    JobService,
    ListArtifacts,
    ListJobs,
    OpenArtifactContent,
    QueryJob,
    SubmitJob,
)

RAW_BODY: Final[Mapping[str, object]] = {
    "instruction": "why do atoms form covalent bonds",
    "context": [{"kind": "LEVEL", "text": "grade 9"}],
}


def command() -> SubmitJobCommand:
    return SubmitJobCommand(
        instruction="why do atoms form covalent bonds",
        context=(),
        constraints=constraints(),
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id=None,
    )


def submit_job(
    *,
    uow: FakeUnitOfWork | None = None,
    admission: AdmittingAdmission | RefusingAdmission | None = None,
) -> tuple[SubmitJob, FakeUnitOfWork]:
    unit = FakeUnitOfWork() if uow is None else uow
    use_case = SubmitJob(
        uow=unit,
        clock=FrozenClock(),
        admission=AdmittingAdmission() if admission is None else admission,
        mint_request_key=lambda: REQUEST_KEY,
    )
    return use_case, unit


# --------------------------------------------------------------------------- admission


async def test_two_active_jobs_still_admits_a_third() -> None:
    repository = FakeJobRepository(active_count=2)
    await ActiveJobLimit(repository, max_active=3).admit(demo_scope())


async def test_three_active_jobs_refuse_the_fourth() -> None:
    repository = FakeJobRepository(active_count=3)
    with pytest.raises(DomainError) as raised:
        await ActiveJobLimit(repository, max_active=3).admit(demo_scope())
    assert raised.value.code is ErrorCode.TOO_MANY_ACTIVE_JOBS


async def test_the_gate_counts_only_the_caller_s_own_jobs() -> None:
    """A capacity gate keyed on the principal, not an ownership check (D090).

    It has to keep filtering by principal even though the scope enforces nothing, or one busy
    user locks everybody else out.
    """
    repository = FakeJobRepository()
    for index in range(3):
        repository.seed(
            make_job(
                job_id=f"job_{index:026d}", principal_id=OTHER_PRINCIPAL, status=JobStatus.QUEUED
            )
        )
    await ActiveJobLimit(repository, max_active=3).admit(demo_scope(PRINCIPAL))


async def test_a_refused_submit_writes_nothing() -> None:
    refusal = RefusingAdmission(error=DomainError(ErrorCode.TOO_MANY_ACTIVE_JOBS))
    use_case, unit = submit_job(admission=refusal)
    with pytest.raises(DomainError):
        await use_case.execute(demo_scope(), command(), RAW_BODY)
    assert refusal.calls == 1
    assert unit.commits == 0
    assert unit.is_empty()


# --------------------------------------------------------------------------- submit


async def test_submit_derives_every_id_off_the_minted_request_key() -> None:
    use_case, unit = submit_job()
    job = await use_case.execute(demo_scope(), command(), RAW_BODY)

    assert job.request_key == REQUEST_KEY
    assert job.job_id == derive_job_id(REQUEST_KEY)
    assert set(unit.work_items) == {derive_work_item_id(job.job_id)}
    assert unit.work_items[derive_work_item_id(job.job_id)].job_id == job.job_id


async def test_submit_starts_queued_at_the_intake_percent() -> None:
    use_case, _ = submit_job()
    job = await use_case.execute(demo_scope(), command(), RAW_BODY)

    assert job.status is JobStatus.QUEUED
    assert job.stage is StageName.INTAKE
    assert job.progress_percent == 10
    assert job.attempt == 0
    assert job.version == 0
    assert job.failure is None
    assert job.artifact_id is None
    assert job.created_at == T0


async def test_submit_stores_the_body_exactly_as_it_arrived() -> None:
    """Scope override item 4: what they typed, kept apart from what we asked for."""
    use_case, unit = submit_job()
    await use_case.execute(demo_scope(), command(), RAW_BODY)

    stored = unit.requests[REQUEST_KEY]
    assert stored.raw == RAW_BODY
    assert stored.principal_id == PRINCIPAL
    assert stored.received_at == T0


async def test_two_identical_submits_are_two_jobs() -> None:
    """No idempotency anywhere (scope override item 2). The key is minted, never accepted."""
    unit = FakeUnitOfWork()
    keys = iter(["req_" + "0" * 26, "req_" + "1" * 26])
    use_case = SubmitJob(
        uow=unit,
        clock=FrozenClock(),
        admission=AdmittingAdmission(),
        mint_request_key=lambda: next(keys),
    )
    first = await use_case.execute(demo_scope(), command(), RAW_BODY)
    second = await use_case.execute(demo_scope(), command(), RAW_BODY)

    assert first.job_id != second.job_id
    assert len(unit.jobs) == 2


async def test_a_failed_transaction_leaves_no_row_behind() -> None:
    """The three rows go through one call, so a half-written submit has nowhere to exist."""
    unit = FakeUnitOfWork(fail_on_commit=True)
    use_case, _ = submit_job(uow=unit)

    with pytest.raises(UnitOfWorkFailure):
        await use_case.execute(demo_scope(), command(), RAW_BODY)

    assert unit.commits == 1
    assert unit.is_empty()


# --------------------------------------------------------------------------- reads


async def test_query_returns_the_job() -> None:
    repository = FakeJobRepository()
    repository.seed(make_job())
    job = await QueryJob(repository).execute(demo_scope(), JOB_ID)
    assert job.job_id == JOB_ID


async def test_query_maps_a_missing_job_to_its_code() -> None:
    with pytest.raises(DomainError) as raised:
        await QueryJob(FakeJobRepository()).execute(demo_scope(), JOB_ID)
    assert raised.value.code is ErrorCode.JOB_NOT_FOUND


async def test_any_caller_reads_any_job() -> None:
    """@audit no ownership check. This asserts the hole rather than hiding it.

    `docs/demo.md` expected a second user id to get `404`; the scope override removed the check,
    so the demo answers `200` instead. Restoring authorisation flips this test.
    """
    repository = FakeJobRepository()
    repository.seed(make_job(principal_id=OTHER_PRINCIPAL))
    scope = demo_scope(PRINCIPAL)
    assert scope.ownership_enforced is False

    job = await QueryJob(repository).execute(scope, JOB_ID)
    assert job.principal_id == OTHER_PRINCIPAL


async def test_listing_jobs_clamps_an_oversized_limit() -> None:
    repository = FakeJobRepository()
    for index in range(5):
        repository.seed(make_job(job_id=f"job_{index:026d}"))
    page = await ListJobs(repository, max_limit=2).execute(demo_scope(), cursor=None, limit=100)
    assert len(page.items) == 2
    assert page.next_cursor is not None


async def test_listing_jobs_rejects_a_limit_below_one() -> None:
    with pytest.raises(DomainError) as raised:
        await ListJobs(FakeJobRepository()).execute(demo_scope(), cursor=None, limit=0)
    assert raised.value.code is ErrorCode.INVALID_REQUEST


async def test_listing_artifacts_filters_by_job() -> None:
    reader = FakeArtifactReader()
    reader.seed(make_artifact())
    reader.seed(make_artifact(artifact_id="art_" + "1" * 26, job_id="job_" + "9" * 26))

    page = await ListArtifacts(reader).execute(demo_scope(), job_id=JOB_ID, cursor=None, limit=10)
    assert [row.artifact_id for row in page.items] == [ARTIFACT_ID]


async def test_opening_content_returns_a_stream() -> None:
    reader = FakeArtifactReader()
    reader.seed(make_artifact())
    stream = await OpenArtifactContent(reader).execute(demo_scope(), ARTIFACT_ID)
    assert stream.media_type == "video/mp4"


async def test_opening_a_missing_artifact_maps_to_its_code() -> None:
    with pytest.raises(DomainError) as raised:
        await OpenArtifactContent(FakeArtifactReader()).execute(demo_scope(), ARTIFACT_ID)
    assert raised.value.code is ErrorCode.ARTIFACT_NOT_FOUND


# --------------------------------------------------------------------------- the api seam


async def test_the_job_service_answers_the_shape_the_api_asks_for() -> None:
    repository = FakeJobRepository()
    repository.seed(make_job())
    use_case, unit = submit_job()
    service = JobService(
        submit=use_case,
        query=QueryJob(repository),
        listing=ListJobs(repository),
    )
    scope = demo_scope()

    submitted = await service.submit(scope, command(), RAW_BODY)
    assert submitted.job_id in unit.jobs
    assert (await service.get(scope, JOB_ID)).job_id == JOB_ID
    assert len((await service.list(scope, cursor=None, limit=10)).items) == 1


async def test_the_artifact_service_answers_the_shape_the_api_asks_for() -> None:
    reader = FakeArtifactReader()
    reader.seed(make_artifact())
    service = ArtifactService(listing=ListArtifacts(reader), content=OpenArtifactContent(reader))
    scope = demo_scope()

    page = await service.list(scope, job_id=None, cursor=None, limit=10)
    assert len(page.items) == 1
    assert (await service.open_content(scope, ARTIFACT_ID)).size_bytes == 1024
