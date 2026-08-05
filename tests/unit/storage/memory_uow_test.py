"""One submit writes three rows, or it writes none.

`Submission` exists so that a half-written submit is not a shape a caller can express. That only
buys anything if the transaction behind it is real, so the failure is injected at each of the
three points in turn and the tables are checked afterwards. The last point is the one that
matters: two rows staged, the third refused, and nothing in the database.
"""

import pytest
from memory_rows_test import PRINCIPAL, T0, job_id_for, make_submission, request_key_for

from app.storage.memory import (
    CommitPoint,
    IntegrityError,
    MemoryDatabase,
    MemoryUnitOfWork,
)


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


async def test_a_submission_writes_the_request_the_job_and_the_work_item(
    database: MemoryDatabase,
) -> None:
    submission = make_submission()

    await MemoryUnitOfWork(database).commit_submission(submission)

    assert database.requests[request_key_for(0)] == submission.request
    assert database.jobs[job_id_for(0)] == submission.job
    row = database.work_items[submission.work_item.item_id]
    assert row.job_id == job_id_for(0)
    assert row.available_at == T0
    assert row.claimed_by is None
    assert row.claim_count == 0


@pytest.mark.parametrize("point", list(CommitPoint))
async def test_a_failure_anywhere_in_the_transaction_leaves_nothing_behind(
    database: MemoryDatabase, point: CommitPoint
) -> None:
    unit = MemoryUnitOfWork(database, fail_after=point)

    with pytest.raises(RuntimeError):
        await unit.commit_submission(make_submission())

    assert database.requests == {}
    assert database.jobs == {}
    assert database.work_items == {}
    assert unit.commits == 1


async def test_a_repeated_submission_is_refused_and_changes_nothing(
    database: MemoryDatabase,
) -> None:
    """At-least-once delivery collides on the primary key rather than queueing twice."""
    unit = MemoryUnitOfWork(database)
    await unit.commit_submission(make_submission())
    before = dict(database.jobs)

    with pytest.raises(IntegrityError):
        await unit.commit_submission(make_submission())

    assert database.jobs == before
    assert len(database.requests) == 1
    assert len(database.work_items) == 1


async def test_a_second_submission_by_the_same_principal_lands_beside_the_first(
    database: MemoryDatabase,
) -> None:
    """There is no replay protection: two submits are two jobs (scope override item 2)."""
    unit = MemoryUnitOfWork(database)

    await unit.commit_submission(make_submission(0, principal_id=PRINCIPAL))
    await unit.commit_submission(make_submission(1, principal_id=PRINCIPAL))

    assert len(database.jobs) == 2
    assert len(database.work_items) == 2
    assert {job.principal_id for job in database.jobs.values()} == {PRINCIPAL}
