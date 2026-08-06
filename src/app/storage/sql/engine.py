"""How anything in this package reaches Postgres.

One place that turns `settings.database_url` into a live connection, so no repository writes a
connection string and nothing else in the codebase reads that setting. The health probe lives
here for the same reason: "is the database reachable" is a question about the connection path,
and `app/api/ports.py` declares the shape the router wants without naming this module (D065).

Two facts this file exists to absorb:

- Compose spells `APP_DATABASE_URL` the SQLAlchemy way, `postgresql+psycopg://...`. libpq has
  never heard of the `+psycopg` part and rejects the whole URL, so `to_psycopg_dsn` strips it.
  The setting keeps the SQLAlchemy spelling because that string is also what a developer pastes
  into a tool, and one normalisation here is cheaper than a second correct-looking URL in .env.
- Every repository method is one `async with` over one connection, so a method that must be
  atomic is atomic by construction and no two methods can end up sharing a transaction. Where
  that connection comes from is the engine's business: a pool if `open()` has been called, a
  fresh `connect()` if it has not. A pool has to be opened and closed with the process, so the
  composition root owns the lifetime (`app/main.py`'s lifespan, `app/worker.py`'s `_serve`) and
  this module owns the fact that a caller cannot tell which of the two it got.

Async psycopg needs a selector event loop. The service runs on Linux in compose, where that is
the default and none of this matters. On Windows the default is a `ProactorEventLoop`, which has
no `add_reader`, and every connect raises `InterfaceError` naming the loop it was handed. The
fix is one line at the point where the loop is created, and it is a property of the entrypoint
rather than of this module:

    asyncio.run(serve(), loop_factory=asyncio.SelectorEventLoop)

Both entrypoints do exactly that, and `tests/contract/conftest.py` does it through
pytest-asyncio's loop factory hook, which is what lets the SQL contract suite run on a Windows
checkout. `asyncio.set_event_loop_policy` would also work and is deprecated for removal in 3.16,
so it is not what any of the three uses.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from app.config import settings

__all__ = ["Dsn", "SqlConnection", "SqlDatabaseProbe", "SqlEngine", "to_psycopg_dsn"]

type Dsn = str
"""A libpq connection string: either a URL or a key-value conninfo."""

type SqlConnection = psycopg.AsyncConnection[TupleRow]

_SCHEME_SEPARATOR: Final[str] = "://"
_DRIVER_SEPARATOR: Final[str] = "+"

CONNECT_TIMEOUT_SECONDS: Final[int] = 5
"""A health check that hangs is worse than one that answers "down".

Without it libpq waits on the OS TCP timeout, which is long enough that a load balancer gives
up on `/health` before the probe does and the service is marked unhealthy for the wrong reason.
"""

POOL_MIN_SIZE: Final[int] = 1
POOL_MAX_SIZE: Final[int] = 8
"""Connections one process may hold.

The floor is one because both containers must be able to start against a database that is up but
idle, and the ceiling is small because there are two of them and Postgres's own default is a
hundred backends for the whole server. The API is I/O bound on a per-request read; the worker
runs one job at a time by construction (`app/worker.py`). Neither needs a wide pool, and a wide
one is only a faster way to exhaust the server.
"""


def to_psycopg_dsn(url: str) -> Dsn:
    """Drop the SQLAlchemy driver token from the scheme; leave everything else byte for byte.

    Only the part before `://` is touched, so a `+` inside a password or a database name stays
    where it is. A string with no scheme at all is a key-value conninfo and passes through.
    """
    scheme, separator, rest = url.partition(_SCHEME_SEPARATOR)
    if not separator:
        return url
    dialect, _, _driver = scheme.partition(_DRIVER_SEPARATOR)
    return f"{dialect}{separator}{rest}"


class SqlEngine:
    """The connection factory. Construct once in the composition root and pass it around.

    Constructing one connects to nothing: it holds a DSN and, once `open()` has been awaited, a
    pool. That ordering is what lets both containers import cleanly while Postgres is still
    starting, and what lets a test build an engine against an address nobody is listening on.

    Unpooled is the fallback rather than the default-forever. Every call opening its own
    connection costs a TCP handshake and a backend fork on the server, which is fine for a test
    that makes four calls and wrong for a process serving requests. `open()` and `close()` are
    the composition root's to call, because a pool's lifetime is the process's.
    """

    def __init__(self, *, dsn: Dsn | None = None) -> None:
        self._dsn: Final[Dsn] = to_psycopg_dsn(dsn if dsn is not None else settings.database_url)
        self._pool: AsyncConnectionPool[SqlConnection] | None = None

    @property
    def dsn(self) -> Dsn:
        """@audit carries the password. Never log it, never put it in an error message."""
        return self._dsn

    @property
    def pooled(self) -> bool:
        return self._pool is not None

    async def open(self) -> None:
        """Bring up the pool. Idempotent, so a double-started lifespan is not a second pool.

        `wait=True` is the point: it returns when `POOL_MIN_SIZE` connections are actually
        established, so a container that reports itself started has proved it can reach the
        database rather than deferring the discovery to the first request. Compose already waits
        on `db` being healthy, and this is the same promise made from our side.
        """
        if self._pool is not None:
            return
        pool: AsyncConnectionPool[SqlConnection] = AsyncConnectionPool(
            self._dsn,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            kwargs={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
            open=False,
        )
        await pool.open(wait=True)
        self._pool = pool

    async def close(self) -> None:
        """Return every connection to the server. Idempotent, and safe to call unopened."""
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[SqlConnection]:
        """One connection, committed on a clean exit and rolled back on an exception.

        That is psycopg's own context manager behaviour, pooled or not, and it is the transaction
        boundary this codebase uses: a repository method that must be atomic takes the connection
        and does its work inside one `async with`.
        """
        pool = self._pool
        if pool is None:
            async with await psycopg.AsyncConnection.connect(
                self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS
            ) as connection:
                yield connection
            return
        async with pool.connection() as connection:
            yield connection


class SqlDatabaseProbe:
    """`GET /health`'s one question, satisfying `app.api.ports.DatabaseProbe`.

    Reachable means a connection opened and the server answered a query, not that a pool object
    exists. Anything less reports healthy while every request 500s.
    """

    def __init__(self, engine: SqlEngine) -> None:
        self._engine: Final[SqlEngine] = engine

    async def ping(self) -> bool:
        """False rather than an exception: an unreachable database is an answer `/health` gives.

        `OperationalError` and nothing wider, because that is the class libpq raises when the
        server is down, refusing, or slow. A misconfigured driver raises `InterfaceError`, which
        is also a `psycopg.Error` and is not an outage: reporting it as one sends an operator to
        restart a database that was never asked for a connection. Same for our own bugs.

        A pooled engine answers the same way for free: `PoolTimeout` and `PoolClosed` are both
        `OperationalError` subclasses, so a pool that cannot hand out a connection reads as an
        unreachable database rather than escaping as a `500` from the health endpoint.
        """
        try:
            async with self._engine.connection() as connection:
                await connection.execute("SELECT 1")
        except psycopg.OperationalError:
            return False
        return True
