"""The two storage backends behind one shape, so the contract suite never names either.

`StorageBackend` is every port a test needs plus the clock they all share. A contract test takes
one, drives it through the ports, and never learns whether it is talking to five dictionaries or
to Postgres. That is the point of the suite: the doubles in `app/storage/memory/` stand in for
the adapters everywhere the adapters are not, and a double that has quietly drifted is a green
suite about a system nobody is running.

Two fields on it are not ports, and both are named for what they are. `hold` puts a work item
into the state a dead worker leaves behind -- claimed, counted, with a lease running out -- which
no port can reach, because reaching it means a process that never came back. `peek` reads the
claim columns, which no port returns either: `ClaimedWorkItem` is what a holder is told, not what
a sweep sees. Everything else in the suite goes through the ports, and these two exist so the
tests about a crash do not have to cause one.

They are also the only SQL written outside `app/storage/sql/`. The rule in
`tests/unit/storage/sql_discipline_test.py` is about the application -- a query inlined in a use
case is a database call nobody can find -- and these three statements are a test fixture arranging
a state and reading it back. Writing them through the adapters instead would mean the suite
proving an adapter correct with the same adapter.

The SQL half is skipped rather than failed when Postgres is not there. `uv run pytest` on a
checkout with no Docker still passes, and `make up` is what turns the other half on.
`APP_TEST_DATABASE_URL` overrides the address; the default is compose's `db` service as seen from
the host, which is where it is after `make up`.

@TODO that default is also a collision. The compose `worker` polls `work_items` in the same
database every `APP_WORK_CLAIM_POLL_SECONDS`, so with the stack running it occasionally claims a
row the queue suite has just inserted and the lease assertions fail on a claim they did not make.
The suite is correct and the environment is shared. The fix is a database of its own -- a second
`POSTGRES_DB` in compose and that name in `DEFAULT_TEST_DSN` -- which lands with the end-to-end
harness (`tests/integration/`), because that is the run which has to be able to trust both.
"""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import psycopg

from app.access.ports import PrincipalRepository
from app.custody.ports import ArtifactRepository
from app.domain.ids import WorkItemId
from app.intake.ports import BriefRepository
from app.orchestration.ports import JobRepository, RequestStore, UnitOfWork, WorkQueue
from app.storage.memory import (
    FrozenClock,
    MemoryArtifactRepository,
    MemoryBriefRepository,
    MemoryDatabase,
    MemoryJobRepository,
    MemoryPrincipalRepository,
    MemoryRequestStore,
    MemoryUnitOfWork,
    MemoryWorkQueue,
    WorkItemRow,
)
from app.storage.sql.engine import SqlEngine, to_psycopg_dsn
from app.storage.sql.migrate import apply_migrations
from app.storage.sql.queue import SqlWorkQueue
from app.storage.sql.repositories import (
    SqlArtifactRepository,
    SqlBriefRepository,
    SqlJobRepository,
    SqlPrincipalRepository,
    SqlRequestStore,
    SqlUnitOfWork,
)
from support.rows import T0

MEMORY: Final[str] = "memory"
SQL: Final[str] = "sql"
BACKENDS: Final[tuple[str, ...]] = (MEMORY, SQL)

DSN_VARIABLE: Final[str] = "APP_TEST_DATABASE_URL"
DEFAULT_TEST_DSN: Final[str] = "postgresql://app:app@localhost:5432/app"
"""Compose's `db` service on its published port. `make up`, and this address is live.

Not `settings.database_url`, which says `@db:5432` -- a name that resolves on the compose network
and not on the host the tests run on.
"""

CONNECT_TIMEOUT_SECONDS: Final[int] = 3

TABLES: Final[str] = "artifacts, work_items, jobs, briefs, requests, principals"
"""Every table the suite writes. `TRUNCATE` takes them together, so no order is implied."""


type HoldRow = Callable[..., Awaitable[None]]
type PeekRow = Callable[[WorkItemId], Awaitable[WorkItemRow | None]]


@dataclass(frozen=True, slots=True)
class StorageBackend:
    """One backend's ports, plus the two escape hatches the module docstring explains."""

    name: str
    clock: FrozenClock
    principals: PrincipalRepository
    requests: RequestStore
    briefs: BriefRepository
    jobs: JobRepository
    unit_of_work: UnitOfWork
    queue: WorkQueue
    artifacts: ArtifactRepository

    hold: HoldRow
    """`hold(item_id, owner=..., lease_left=..., claim_count=...)`, the dead worker's leftovers.

    `lease_left` is seconds after `T0`, so a test moves the clock to cross the deadline rather
    than doing arithmetic on a datetime it never sees.
    """

    peek: PeekRow
    """The `work_items` row with its claim columns, or `None`."""


def memory_backend() -> StorageBackend:
    """Five dictionaries behind one `MemoryDatabase`, because the doubles are not independent."""
    database = MemoryDatabase()
    clock = FrozenClock(at=T0)

    async def hold(item_id: WorkItemId, *, owner: str, lease_left: int, claim_count: int) -> None:
        row = database.work_items[item_id]
        database.work_items[item_id] = row.claimed(
            owner=owner,
            until=T0 + timedelta(seconds=lease_left),
            claim_count=claim_count,
        )

    async def peek(item_id: WorkItemId) -> WorkItemRow | None:
        return database.work_items.get(item_id)

    return StorageBackend(
        name=MEMORY,
        clock=clock,
        principals=MemoryPrincipalRepository(database, clock),
        requests=MemoryRequestStore(database),
        briefs=MemoryBriefRepository(database),
        jobs=MemoryJobRepository(database, clock),
        unit_of_work=MemoryUnitOfWork(database),
        queue=MemoryWorkQueue(database, clock),
        artifacts=MemoryArtifactRepository(database),
        hold=hold,
        peek=peek,
    )


def sql_backend(dsn: str) -> StorageBackend:
    """The same ports over Postgres, on a schema `prepare_database` has already migrated."""
    engine = SqlEngine(dsn=dsn)
    clock = FrozenClock(at=T0)

    async def hold(item_id: WorkItemId, *, owner: str, lease_left: int, claim_count: int) -> None:
        async with engine.connection() as connection:
            await connection.execute(
                "UPDATE work_items SET claimed_by = %(owner)s, claimed_until = %(until)s, "
                "claim_count = %(claim_count)s WHERE item_id = %(item_id)s",
                {
                    "owner": owner,
                    "until": T0 + timedelta(seconds=lease_left),
                    "claim_count": claim_count,
                    "item_id": item_id,
                },
            )

    async def peek(item_id: WorkItemId) -> WorkItemRow | None:
        async with engine.connection() as connection:
            cursor = await connection.execute(
                "SELECT item_id, job_id, available_at, created_at, claimed_by, claimed_until, "
                "claim_count FROM work_items WHERE item_id = %(item_id)s",
                {"item_id": item_id},
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return WorkItemRow(
            item_id=row[0],
            job_id=row[1],
            available_at=row[2],
            created_at=row[3],
            claimed_by=row[4],
            claimed_until=row[5],
            claim_count=row[6],
        )

    return StorageBackend(
        name=SQL,
        clock=clock,
        principals=SqlPrincipalRepository(engine, clock),
        requests=SqlRequestStore(engine),
        briefs=SqlBriefRepository(engine),
        jobs=SqlJobRepository(engine, clock),
        unit_of_work=SqlUnitOfWork(engine),
        queue=SqlWorkQueue(engine, clock),
        artifacts=SqlArtifactRepository(engine),
        hold=hold,
        peek=peek,
    )


def configured_dsn() -> str:
    return os.environ.get(DSN_VARIABLE) or DEFAULT_TEST_DSN


def prepare_database(dsn: str) -> str | None:
    """Migrate the test database, or say why the SQL half of the suite cannot run.

    `None` on success, a skip reason otherwise. An unreachable database is a skip rather than a
    failure: the suite has to pass on a checkout with no Docker, and the memory half still runs
    every assertion in it. A database that answers and then refuses the migration is a different
    thing and is not swallowed -- `apply_migrations` raises, because a broken schema should stop
    the run rather than quietly halve it.
    """
    try:
        with psycopg.connect(
            to_psycopg_dsn(dsn), connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as connection:
            connection.execute("SELECT 1")
    except psycopg.OperationalError as error:
        return f"no Postgres at {dsn}: {str(error).strip().splitlines()[0]}"
    apply_migrations(dsn)
    return None


def truncate(dsn: str) -> None:
    """Empty every table between tests. Leaves the schema and the migration bookkeeping alone."""
    with psycopg.connect(to_psycopg_dsn(dsn), autocommit=True) as connection:
        connection.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
