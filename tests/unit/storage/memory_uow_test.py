"""Fault injection, which only the double can do.

That a submit writes three rows or none is a contract property and lives in
`tests/contract/submission_test.py`, run against both backends. What cannot move there is the
proof that the swap is what commits: a test cannot make a dictionary run out of disk, so
`MemoryUnitOfWork` takes a `CommitPoint` and refuses at each of the three positions in turn.

The last point is the one that matters. Two rows staged, the third refused, and nothing in the
database -- which is only true if the writes went to copies and the assignment at the end is the
commit.
"""

import pytest
from memory_rows_test import make_submission

from app.storage.memory import CommitPoint, MemoryDatabase, MemoryUnitOfWork


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


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
