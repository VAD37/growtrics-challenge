"""The `jobs` table double: transitions, the admission count, and keyset paging.

Three properties carry the weight here. A transition matches on the version or it is refused, so
a lost update is a failed test rather than a status nobody can explain. `None` on a nullable
column means "leave it alone" and never "clear it", which is the easiest bug in the file to
write and the hardest to see afterwards. And a page is positioned by key, so a row inserted while
a client is paging cannot make it skip or repeat one.
"""

from datetime import timedelta

import pytest
from memory_rows_test import (
    OTHER_PRINCIPAL,
    PRINCIPAL,
    T0,
    demo_scope,
    job_id_for,
    make_job,
    make_request,
    make_submission,
    request_key_for,
)

from app.domain.enums import JobStatus, StageName, percent_for
from app.domain.errors import ERROR_CATALOG, ErrorCode
from app.domain.ids import derive_brief_id, derive_trace_id
from app.domain.records import Cursor, FailureRecord, JobRecord
from app.orchestration.ports import JobTransition
from app.storage.memory import (
    FrozenClock,
    MemoryDatabase,
    MemoryJobRepository,
    MemoryRequestStore,
    MemoryUnitOfWork,
    RowNotFoundError,
    VersionConflictError,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(at=T0)


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def jobs(database: MemoryDatabase, clock: FrozenClock) -> MemoryJobRepository:
    return MemoryJobRepository(database, clock)


def transition_to(
    job: JobRecord,
    *,
    status: JobStatus = JobStatus.RUNNING,
    stage: StageName = StageName.PREPARING,
    expected_version: int | None = None,
    brief_id: str | None = None,
    artifact_id: str | None = None,
    failure: FailureRecord | None = None,
) -> JobTransition:
    return JobTransition(
        job_id=job.job_id,
        expected_version=job.version if expected_version is None else expected_version,
        status=status,
        stage=stage,
        progress_percent=percent_for(stage),
        brief_id=brief_id,
        artifact_id=artifact_id,
        failure=failure,
    )


def a_failure() -> FailureRecord:
    return FailureRecord(
        code=ErrorCode.GENERATION_FAILED,
        stage=StageName.GENERATING,
        message=ERROR_CATALOG[ErrorCode.GENERATION_FAILED].message,
        retryable=False,
        occurred_at=T0,
        trace_id=derive_trace_id(job_id_for(0), 0),
    )


async def test_a_matching_version_moves_the_row_and_stamps_the_clock(
    database: MemoryDatabase, jobs: MemoryJobRepository, clock: FrozenClock
) -> None:
    job = make_job()
    database.jobs[job.job_id] = job
    clock.advance(30)

    updated = await jobs.apply_transition(transition_to(job))

    assert updated.status is JobStatus.RUNNING
    assert updated.stage is StageName.PREPARING
    assert updated.progress_percent == percent_for(StageName.PREPARING)
    assert updated.version == 1
    assert updated.updated_at == T0 + timedelta(seconds=30)
    assert updated.created_at == T0
    assert database.jobs[job.job_id] == updated


async def test_a_stale_version_is_refused_and_the_row_is_untouched(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    job = make_job()
    database.jobs[job.job_id] = job
    moved = await jobs.apply_transition(transition_to(job))

    with pytest.raises(VersionConflictError):
        await jobs.apply_transition(transition_to(job, expected_version=0))

    assert database.jobs[job.job_id] == moved
    assert database.jobs[job.job_id].version == 1


async def test_a_transition_against_a_missing_row_is_refused(jobs: MemoryJobRepository) -> None:
    with pytest.raises(RowNotFoundError):
        await jobs.apply_transition(transition_to(make_job()))


async def test_none_leaves_the_brief_the_artifact_and_the_failure_alone(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    """The single easiest bug in this file: `None` meaning "clear it" rather than "skip it"."""
    job = make_job()
    database.jobs[job.job_id] = job
    brief_id = derive_brief_id(job.job_id)
    failure = a_failure()

    filled = await jobs.apply_transition(
        transition_to(job, brief_id=brief_id, artifact_id="art_" + "0" * 26, failure=failure)
    )
    assert filled.brief_id == brief_id

    later = await jobs.apply_transition(
        transition_to(filled, status=JobStatus.RUNNING, stage=StageName.GENERATING)
    )

    assert later.brief_id == brief_id
    assert later.artifact_id == "art_" + "0" * 26
    assert later.failure == failure


async def test_count_active_keys_on_the_principal_whatever_the_scope_says(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    """A capacity gate, not an ownership predicate (D090)."""
    for index, status in enumerate((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED)):
        job = make_job(index, status=status)
        database.jobs[job.job_id] = job
    theirs = make_job(5, principal_id=OTHER_PRINCIPAL, status=JobStatus.QUEUED)
    database.jobs[theirs.job_id] = theirs

    assert await jobs.count_active(demo_scope(OTHER_PRINCIPAL), PRINCIPAL) == 2
    assert await jobs.count_active(demo_scope(PRINCIPAL), OTHER_PRINCIPAL) == 1


async def test_reads_apply_no_ownership_predicate(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    """@audit the demo's whole authorisation model, asserted rather than described."""
    theirs = make_job(1, principal_id=OTHER_PRINCIPAL)
    database.jobs[theirs.job_id] = theirs

    found = await jobs.get(demo_scope(PRINCIPAL), theirs.job_id)
    listed = await jobs.list(demo_scope(PRINCIPAL), cursor=None, limit=10)

    assert found == theirs
    assert listed.items == (theirs,)


async def test_load_for_run_reads_the_row_without_a_scope(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    job = make_job()
    database.jobs[job.job_id] = job

    assert await jobs.load_for_run(job.job_id) == job
    assert await jobs.load_for_run(job_id_for(9)) is None


async def test_listing_is_newest_first_and_pages_by_keyset(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    for index in range(5):
        job = make_job(index, created_at=T0 + timedelta(minutes=index))
        database.jobs[job.job_id] = job

    first = await jobs.list(demo_scope(), cursor=None, limit=2)
    assert [job.job_id for job in first.items] == [job_id_for(4), job_id_for(3)]
    assert first.next_cursor is not None

    second = await jobs.list(demo_scope(), cursor=Cursor.decode(first.next_cursor), limit=2)
    assert [job.job_id for job in second.items] == [job_id_for(2), job_id_for(1)]
    assert second.next_cursor is not None

    last = await jobs.list(demo_scope(), cursor=Cursor.decode(second.next_cursor), limit=2)
    assert [job.job_id for job in last.items] == [job_id_for(0)]
    assert last.next_cursor is None


async def test_a_row_inserted_mid_pagination_neither_skips_nor_duplicates(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    """The reason the cursor is a key and not an offset (D072)."""
    for index in range(4):
        job = make_job(index, created_at=T0 + timedelta(minutes=index))
        database.jobs[job.job_id] = job

    first = await jobs.list(demo_scope(), cursor=None, limit=2)
    assert first.next_cursor is not None

    newcomer = make_job(9, created_at=T0 + timedelta(hours=1))
    database.jobs[newcomer.job_id] = newcomer

    second = await jobs.list(demo_scope(), cursor=Cursor.decode(first.next_cursor), limit=2)

    seen = [job.job_id for job in first.items + second.items]
    assert seen == [job_id_for(3), job_id_for(2), job_id_for(1), job_id_for(0)]
    assert len(set(seen)) == len(seen)
    assert newcomer.job_id not in seen


async def test_two_rows_written_in_the_same_microsecond_still_have_an_order(
    database: MemoryDatabase, jobs: MemoryJobRepository
) -> None:
    """The id is the tiebreaker, so a page boundary between them loses neither."""
    for index in range(3):
        job = make_job(index, created_at=T0)
        database.jobs[job.job_id] = job

    first = await jobs.list(demo_scope(), cursor=None, limit=2)
    assert first.next_cursor is not None
    second = await jobs.list(demo_scope(), cursor=Cursor.decode(first.next_cursor), limit=2)

    seen = [job.job_id for job in first.items + second.items]
    assert sorted(seen) == sorted(job_id_for(index) for index in range(3))
    assert len(set(seen)) == 3


async def test_the_request_store_reads_what_the_transaction_wrote(
    database: MemoryDatabase,
) -> None:
    requests = MemoryRequestStore(database)
    assert await requests.load(request_key_for(0)) is None

    await MemoryUnitOfWork(database).commit_submission(make_submission())

    assert await requests.load(request_key_for(0)) == make_request()
