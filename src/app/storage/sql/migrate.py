"""Apply the migrations. Run before anything serves: `python -m app.storage.sql.migrate`.

Compose runs it as the first half of both container commands, so a reviewer's `make up` brings
up a schema without a second step and without a one-off shell into a container.

Why there is no migration framework here. Alembic buys autogeneration from an ORM's metadata,
a branching version graph, and downgrades. This repository has one hand-written `.sql` file, no
ORM metadata to diff against, and a schema that is frozen (D071); the down direction for a
frozen schema is `drop the database`. What is actually needed is a bookkeeping table and a lock,
which is the forty lines below. When a second engineer needs branching versions, this is a
small enough file to throw away in favour of Alembic without unpicking anything.

Three properties it does have to hold:

- **Idempotent.** Applying twice applies nothing the second time. Both containers run it.
- **Atomic.** One transaction covers the DDL and the bookkeeping row together. Postgres makes
  DDL transactional, so a failure halfway leaves no half-built schema and no row claiming one.
- **Safe when two containers start at once.** `api` and `worker` race on every `make up`.
  A session-level advisory lock, taken before the bookkeeping table is even read, means the
  loser waits and then finds the work already done rather than colliding on a CREATE TABLE.

Checksums are recorded per version. Editing an applied migration is the mistake this catches:
the developer who edits it sees a clean database, and the deployment that already ran it sees
nothing at all. Better a loud failure than two schemas that share a version number.
"""

import hashlib
import sys
from dataclasses import dataclass
from importlib.resources import files
from typing import Final

import psycopg

from app.config import settings
from app.storage.sql.engine import Dsn, to_psycopg_dsn

__all__ = ["MIGRATIONS", "Migration", "MigrationError", "apply_migrations"]

type AppliedVersions = tuple[str, ...]

_ADVISORY_LOCK_KEY: Final[int] = 0x67525443
"""Arbitrary and fixed. Any two processes running this module must pick the same number."""

_BOOKKEEPING_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The database and the migration files disagree about history."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One `.sql` file and the version it is recorded under.

    Read at run time rather than imported, because the file ships as package data inside the
    image and the version string is what the database stores. Sorting is by `version`, so the
    next one is `0002_` and the order never depends on a directory listing.
    """

    version: str
    filename: str

    def read(self) -> str:
        return (files(__package__) / self.filename).read_text(encoding="utf-8")

    def checksum(self) -> str:
        return hashlib.sha256(self.read().encode("utf-8")).hexdigest()


MIGRATIONS: Final[tuple[Migration, ...]] = (Migration(version="0001", filename="schema.sql"),)


def apply_migrations(dsn: Dsn) -> AppliedVersions:
    """Bring the database up to the last migration. Returns the versions this call applied.

    An empty tuple means the database was already current, which is the normal result on every
    start after the first. Raises `MigrationError` when an applied migration's file has changed
    since, and `psycopg.Error` when the database refuses the work.
    """
    applied: list[str] = []
    with psycopg.connect(to_psycopg_dsn(dsn)) as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
        connection.execute(_BOOKKEEPING_DDL)
        recorded: dict[str, str] = dict(
            connection.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        )
        for migration in MIGRATIONS:
            checksum = migration.checksum()
            previous = recorded.get(migration.version)
            if previous is not None:
                if previous != checksum:
                    raise MigrationError(
                        f"migration {migration.version} was applied with a different checksum;"
                        f" {migration.filename} has been edited since"
                    )
                continue
            connection.execute(migration.read())
            connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, checksum),
            )
            applied.append(migration.version)
    return tuple(applied)


def main() -> int:
    """The entrypoint compose calls. Prints what happened; non-zero means do not start.

    @audit the DSN is never printed. It carries the database password and this output goes to a
    container log that anybody reading `docker compose logs` sees.
    """
    try:
        applied = apply_migrations(settings.database_url)
    except (psycopg.Error, MigrationError) as error:
        print(f"migrate: failed: {error}", file=sys.stderr)
        return 1
    if applied:
        print(f"migrate: applied {', '.join(applied)}")
    else:
        print("migrate: already at the latest migration, nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
