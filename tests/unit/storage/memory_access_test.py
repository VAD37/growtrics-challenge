"""`principals` and the health probe: the two smallest doubles, and both have a behaviour.

The principal row is the foreign key every other table points at, so the thing worth asserting
is that a second sighting of the same caller does not mint a second row or move the first one's
`created_at`. The probe matters because it can say no, which is the branch `GET /health` exists
to render.
"""

import pytest
from memory_rows_test import OTHER_PRINCIPAL, PRINCIPAL, T0

from app.storage.memory import (
    FrozenClock,
    MemoryDatabase,
    MemoryDatabaseProbe,
    MemoryPrincipalRepository,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(at=T0)


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def principals(database: MemoryDatabase, clock: FrozenClock) -> MemoryPrincipalRepository:
    return MemoryPrincipalRepository(database, clock)


async def test_the_first_request_a_caller_makes_admits_them(
    database: MemoryDatabase, principals: MemoryPrincipalRepository
) -> None:
    principal = await principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)

    assert principal.principal_id == PRINCIPAL
    assert principal.external_id == PRINCIPAL
    assert principal.created_at == T0
    assert database.principals == {PRINCIPAL: principal}


async def test_a_second_sighting_keeps_the_row_and_its_created_at(
    database: MemoryDatabase, principals: MemoryPrincipalRepository, clock: FrozenClock
) -> None:
    first = await principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)
    clock.advance(3600)

    again = await principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)

    assert again == first
    assert again.created_at == T0
    assert len(database.principals) == 1


async def test_two_callers_get_two_rows(principals: MemoryPrincipalRepository) -> None:
    mine = await principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)
    theirs = await principals.upsert(principal_id=OTHER_PRINCIPAL, external_id=OTHER_PRINCIPAL)

    assert mine.principal_id != theirs.principal_id


async def test_the_probe_reports_what_the_database_says(database: MemoryDatabase) -> None:
    probe = MemoryDatabaseProbe(database)
    assert await probe.ping() is True

    database.reachable = False

    assert await probe.ping() is False
