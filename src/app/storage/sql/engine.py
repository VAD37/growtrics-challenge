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
- Every call opens its own connection. @TODO this wants `psycopg_pool` before anything serves
  real traffic; a per-request connect costs a TCP handshake and a fork on the server. It is not
  a dependency yet (scope override: no new runtime dependency), and the demo's traffic is one
  reviewer with curl.

@TODO async psycopg needs a selector event loop. The service runs on Linux in compose, where
that is the default and none of this matters. A developer running `uvicorn` directly on Windows
gets a `ProactorEventLoop` and every connection raises `InterfaceError`, so the composition root
would need `asyncio.WindowsSelectorEventLoopPolicy` before it is worth supporting that.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import psycopg
from psycopg.rows import TupleRow

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

    Holds no state beyond the DSN, so it is safe to build before the database is up: nothing
    connects until someone enters `connection()`. That ordering is what lets the API import
    cleanly while Postgres is still starting.
    """

    def __init__(self, *, dsn: Dsn | None = None) -> None:
        self._dsn: Final[Dsn] = to_psycopg_dsn(dsn if dsn is not None else settings.database_url)

    @property
    def dsn(self) -> Dsn:
        """@audit carries the password. Never log it, never put it in an error message."""
        return self._dsn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[SqlConnection]:
        """One connection, committed on a clean exit and rolled back on an exception.

        That is psycopg's own context manager behaviour and it is the transaction boundary this
        codebase uses: a repository method that must be atomic takes the connection and does its
        work inside one `async with`.
        """
        async with await psycopg.AsyncConnection.connect(
            self._dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS
        ) as connection:
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
        """
        try:
            async with self._engine.connection() as connection:
                await connection.execute("SELECT 1")
        except psycopg.OperationalError:
            return False
        return True
