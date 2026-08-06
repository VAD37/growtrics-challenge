"""The health probe, which has no second backend to be held against.

`MemoryDatabaseProbe` matters because it can say no. An unreachable database is a state
`GET /health` has to render, and a probe that could only answer yes would leave that branch
untested; `SqlDatabaseProbe` answers the same question by failing to connect, which is not a
state a contract test can arrange without taking the database away mid-run.

`PrincipalRepository`'s behaviour moved to `tests/contract/principals_test.py`, where the double
and the SQL adapter are held to it together.
"""

import pytest

from app.storage.memory import MemoryDatabase, MemoryDatabaseProbe


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


async def test_the_probe_reports_what_the_database_says(database: MemoryDatabase) -> None:
    probe = MemoryDatabaseProbe(database)
    assert await probe.ping() is True

    database.reachable = False

    assert await probe.ping() is False
