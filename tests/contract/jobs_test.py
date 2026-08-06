"""`JobRepository`, against both backends.

Four properties carry the weight. A transition matches on the version or it is refused, so a lost
update is a failed test rather than a status nobody can explain. `None` on a nullable column
means "leave it alone" and never "clear it", which is the easiest bug in either adapter to write
and the hardest to see afterwards. A page is positioned by key, so a row inserted while a client
is paging cannot make it skip or repeat one. And `list_untouched_since` reads `updated_at`, so
the sweep gives up on jobs that have stopped moving rather than on jobs that are merely old.
"""

from datetime import timedelta

import pytest
from support.backends import StorageBackend
from support.rows import (
    OTHER_PRINCIPAL,
    PRINCIPAL,
    T0,
    demo_scope,
    job_id_for,
    make_job,
)
from support.seed import submit, touch

from app.domain.enums import JobStatus, StageName, percent_for
from app.domain.errors import ERROR_CATALOG, ErrorCode
from app.domain.ids import derive_brief_id, derive_trace_id
from app.domain.records import Cursor, FailureRecord, JobRecord
from app.orchestration.ports import JobTransition
from app.storage.errors import RowNotFoundError, VersionConflictError

AN_ARTIFACT_ID = "art_" + "0" * 26


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


# --------------------------------------------------------------------------- transitions


async def test_a_matching_version_moves_the_row_and_stamps_the_clock(
    backend: StorageBackend,
) -> None:
    job = await submit(backend)
    backend.clock.advance(30)

    updated = await backend.jobs.apply_transition(transition_to(job))

    assert updated.status is JobStatus.RUNNING
    assert updated.stage is StageName.PREPARING
    assert updated.progress_percent == percent_for(StageName.PREPARING)
    assert updated.version == 1
    assert updated.updated_at == T0 + timedelta(seconds=30)
    assert updated.created_at == T0
    assert await backend.jobs.load_for_run(job.job_id) == updated


async def test_a_stale_version_is_refused_and_the_row_is_untouched(
    backend: StorageBackend,
) -> None:
    job = await submit(backend)
    moved = await backend.jobs.apply_transition(transition_to(job))

    with pytest.raises(VersionConflictError):
        await backend.jobs.apply_transition(transition_to(job, expected_version=0))

    assert await backend.jobs.load_for_run(job.job_id) == moved


async def test_a_transition_against_a_missing_row_is_refused(backend: StorageBackend) -> None:
    with pytest.raises(RowNotFoundError):
        await backend.jobs.apply_transition(transition_to(make_job()))


async def test_none_leaves_the_brief_the_artifact_and_the_failure_alone(
    backend: StorageBackend,
) -> None:
    """The single easiest bug in either adapter: `None` meaning "clear it" rather than "skip"."""
    job = await submit(backend)
    brief_id = derive_brief_id(job.job_id)
    failure = a_failure()

    filled = await backend.jobs.apply_transition(
        transition_to(job, brief_id=brief_id, artifact_id=AN_ARTIFACT_ID, failure=failure)
    )
    assert filled.brief_id == brief_id

    later = await backend.jobs.apply_transition(
        transition_to(filled, status=JobStatus.RUNNING, stage=StageName.GENERATING)
    )

    assert later.brief_id == brief_id
    assert later.artifact_id == AN_ARTIFACT_ID
    assert later.failure == failure


# --------------------------------------------------------------------------- reads


async def test_count_active_keys_on_the_principal_whatever_the_scope_says(
    backend: StorageBackend,
) -> None:
    """A capacity gate, not an ownership predicate (D090)."""
    await submit(backend, 0)
    running = await submit(backend, 1)
    await touch(backend, running, at=T0, status=JobStatus.RUNNING, stage=StageName.PREPARING)
    done = await submit(backend, 2)
    await touch(backend, done, at=T0, status=JobStatus.SUCCEEDED, stage=StageName.DONE)
    await submit(backend, 5, principal_id=OTHER_PRINCIPAL)

    assert await backend.jobs.count_active(demo_scope(OTHER_PRINCIPAL), PRINCIPAL) == 2
    assert await backend.jobs.count_active(demo_scope(PRINCIPAL), OTHER_PRINCIPAL) == 1


async def test_reads_apply_no_ownership_predicate(backend: StorageBackend) -> None:
    """@audit the demo's whole authorisation model, asserted rather than described."""
    theirs = await submit(backend, 1, principal_id=OTHER_PRINCIPAL)

    found = await backend.jobs.get(demo_scope(PRINCIPAL), theirs.job_id)
    listed = await backend.jobs.list(demo_scope(PRINCIPAL), cursor=None, limit=10)

    assert found == theirs
    assert listed.items == (theirs,)


async def test_load_for_run_reads_the_row_without_a_scope(backend: StorageBackend) -> None:
    job = await submit(backend)

    assert await backend.jobs.load_for_run(job.job_id) == job
    assert await backend.jobs.load_for_run(job_id_for(9)) is None


# --------------------------------------------------------------------------- the sweep's read


async def test_untouched_since_is_inclusive_at_the_moment_it_was_given(
    backend: StorageBackend,
) -> None:
    """At the edge the wait is over, which is `policy.lease_expired`'s convention."""
    on_the_edge = await submit(backend, 0)
    later = await submit(backend, 1)
    await touch(backend, later, at=T0 + timedelta(microseconds=1))

    waiting = await backend.jobs.list_untouched_since(JobStatus.QUEUED, T0, limit=10)

    assert [job.job_id for job in waiting] == [on_the_edge.job_id]


async def test_untouched_since_reads_the_last_write_and_not_the_submit(
    backend: StorageBackend,
) -> None:
    """A job that moved a second ago is not untouched, however long ago it was submitted."""
    old_but_moving = await submit(backend, 0, created_at=T0 - timedelta(days=7))
    await touch(backend, old_but_moving, at=T0 + timedelta(minutes=5))
    young_and_stuck = await submit(backend, 1, created_at=T0 + timedelta(minutes=1))

    waiting = await backend.jobs.list_untouched_since(
        JobStatus.QUEUED, T0 + timedelta(minutes=2), limit=10
    )

    assert [job.job_id for job in waiting] == [young_and_stuck.job_id]
    # Without this the test passes on a `created_at` filter too, and proves nothing.
    assert old_but_moving.created_at < young_and_stuck.created_at


async def test_untouched_since_is_oldest_first_and_the_cap_keeps_the_oldest(
    backend: StorageBackend,
) -> None:
    """The backlog is deeper than one tick. Draining its worst end beats reading all of it."""
    for index in range(4):
        await submit(backend, index, created_at=T0 - timedelta(minutes=index))

    waiting = await backend.jobs.list_untouched_since(JobStatus.QUEUED, T0, limit=2)

    assert [job.job_id for job in waiting] == [job_id_for(3), job_id_for(2)]


async def test_untouched_since_never_crosses_into_another_status(
    backend: StorageBackend,
) -> None:
    """Waiting too long to start and hanging mid-run are two timeouts, not one (`sweeper.py`)."""
    an_hour_ago = T0 - timedelta(hours=1)
    queued = await submit(backend, 0, created_at=an_hour_ago)
    running = await submit(backend, 1, created_at=an_hour_ago)
    await touch(
        backend, running, at=an_hour_ago, status=JobStatus.RUNNING, stage=StageName.GENERATING
    )
    done = await submit(backend, 2, created_at=an_hour_ago)
    await touch(backend, done, at=an_hour_ago, status=JobStatus.SUCCEEDED, stage=StageName.DONE)

    waiting = await backend.jobs.list_untouched_since(JobStatus.QUEUED, T0, limit=10)

    assert [job.job_id for job in waiting] == [queued.job_id]


# --------------------------------------------------------------------------- paging


async def test_listing_is_newest_first_and_pages_by_keyset(backend: StorageBackend) -> None:
    for index in range(5):
        await submit(backend, index, created_at=T0 + timedelta(minutes=index))

    first = await backend.jobs.list(demo_scope(), cursor=None, limit=2)
    assert [job.job_id for job in first.items] == [job_id_for(4), job_id_for(3)]
    assert first.next_cursor is not None

    second = await backend.jobs.list(demo_scope(), cursor=Cursor.decode(first.next_cursor), limit=2)
    assert [job.job_id for job in second.items] == [job_id_for(2), job_id_for(1)]
    assert second.next_cursor is not None

    last = await backend.jobs.list(demo_scope(), cursor=Cursor.decode(second.next_cursor), limit=2)
    assert [job.job_id for job in last.items] == [job_id_for(0)]
    assert last.next_cursor is None


async def test_a_row_inserted_mid_pagination_neither_skips_nor_duplicates(
    backend: StorageBackend,
) -> None:
    """The reason the cursor is a key and not an offset (D072)."""
    for index in range(4):
        await submit(backend, index, created_at=T0 + timedelta(minutes=index))

    first = await backend.jobs.list(demo_scope(), cursor=None, limit=2)
    assert first.next_cursor is not None

    newcomer = await submit(backend, 9, created_at=T0 + timedelta(hours=1))

    second = await backend.jobs.list(demo_scope(), cursor=Cursor.decode(first.next_cursor), limit=2)

    seen = [job.job_id for job in first.items + second.items]
    assert seen == [job_id_for(3), job_id_for(2), job_id_for(1), job_id_for(0)]
    assert len(set(seen)) == len(seen)
    assert newcomer.job_id not in seen


async def test_two_rows_written_in_the_same_microsecond_still_have_an_order(
    backend: StorageBackend,
) -> None:
    """The id is the tiebreaker, so a page boundary between them loses neither."""
    for index in range(3):
        await submit(backend, index, created_at=T0)

    first = await backend.jobs.list(demo_scope(), cursor=None, limit=2)
    assert first.next_cursor is not None
    second = await backend.jobs.list(demo_scope(), cursor=Cursor.decode(first.next_cursor), limit=2)

    seen = [job.job_id for job in first.items + second.items]
    assert sorted(seen) == sorted(job_id_for(index) for index in range(3))
    assert len(set(seen)) == 3
