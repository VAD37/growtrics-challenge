"""What migration 0001 is allowed to contain.

The schema is the one artifact in this repository whose change costs a migration and a
coordinated deploy (D071). Everything else in `app/storage/sql/` can be rewritten behind a
port; a column that quietly appears, disappears, or changes nullability cannot. So this file
pins the DDL: table set, column set per table, nullability where a decision turned on it, and
the indexes the access rules are enforced by.

It reads `schema.sql` as text and never opens a connection. A test that needs Postgres runs
somewhere else and not on every commit, which would make the guard optional exactly when it is
inconvenient. The parser is deliberately blunt -- comments stripped, statements split on `;` --
because the file is hand-written DDL and nothing in it needs a real grammar. Stripping comments
first is load-bearing: `schema.sql` names `idempotency_key` and the deferred tables in its
prose, and the absence assertions below have to read the statements rather than the commentary.

The second half of the file covers `engine.py`'s DSN normalisation and probe shape, which are
the two pieces of the connection path that hold a rule rather than a call.
"""

import inspect
import re
from importlib.resources import files
from typing import Final

import pytest

from app.api.ports import DatabaseProbe
from app.storage.sql.engine import SqlDatabaseProbe, SqlEngine, to_psycopg_dsn
from app.storage.sql.migrate import MIGRATIONS

# --------------------------------------------------------------------------- parsing

type TableName = str
type ColumnName = str
type ColumnDefinition = str
type IndexName = str

_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"--[^\n]*")
_TABLE_RE: Final[re.Pattern[str]] = re.compile(r"CREATE TABLE (\w+) \(\s*(.*?)\n\);", re.DOTALL)
_INDEX_RE: Final[re.Pattern[str]] = re.compile(r"CREATE (?:UNIQUE )?INDEX (\w+) ON (\w+)")
_REFERENCES_RE: Final[re.Pattern[str]] = re.compile(r"REFERENCES (\w+)")

_TABLE_CONSTRAINT_PREFIXES: Final[tuple[str, ...]] = (
    "PRIMARY KEY",
    "FOREIGN KEY",
    "UNIQUE",
    "CHECK",
    "CONSTRAINT ",
    "EXCLUDE",
)


def _schema_text() -> str:
    return (files("app.storage.sql") / "schema.sql").read_text(encoding="utf-8")


def _statements(sql: str) -> tuple[str, ...]:
    stripped = _COMMENT_RE.sub("", sql)
    return tuple(part.strip() for part in stripped.split(";") if part.strip())


def _tables(sql: str) -> dict[TableName, dict[ColumnName, ColumnDefinition]]:
    """Table name to its columns, in declaration order, with the definition text kept."""
    found: dict[TableName, dict[ColumnName, ColumnDefinition]] = {}
    for name, body in _TABLE_RE.findall(_COMMENT_RE.sub("", sql)):
        columns: dict[ColumnName, ColumnDefinition] = {}
        for raw_line in body.split("\n"):
            line = raw_line.strip().rstrip(",")
            if not line or line.upper().startswith(_TABLE_CONSTRAINT_PREFIXES):
                continue
            column, _, definition = line.partition(" ")
            columns[column] = definition.strip()
        found[name] = columns
    return found


def _indexes(sql: str) -> dict[IndexName, TableName]:
    return dict(_INDEX_RE.findall(_COMMENT_RE.sub("", sql)))


SCHEMA: Final[str] = _schema_text()
TABLES: Final[dict[TableName, dict[ColumnName, ColumnDefinition]]] = _tables(SCHEMA)
INDEXES: Final[dict[IndexName, TableName]] = _indexes(SCHEMA)

# --------------------------------------------------------------------------- the six tables

EXPECTED_COLUMNS: Final[dict[TableName, frozenset[ColumnName]]] = {
    "principals": frozenset({"principal_id", "external_id", "created_at"}),
    "requests": frozenset({"request_key", "principal_id", "raw", "received_at"}),
    "briefs": frozenset(
        {
            "brief_id",
            "job_id",
            "brief_hash",
            "template_version",
            "subject",
            "concept_id",
            "instruction",
            "context_items",
            "constraints",
            "guard_verdict",
            "sealed_at",
        }
    ),
    "jobs": frozenset(
        {
            "job_id",
            "request_key",
            "principal_id",
            "chat_context_id",
            "brief_id",
            "status",
            "stage",
            "attempt",
            "progress_percent",
            "profile",
            "output_contract",
            "constraints",
            "artifact_id",
            "budget",
            "failure",
            "version",
            "created_at",
            "updated_at",
        }
    ),
    "work_items": frozenset(
        {
            "item_id",
            "job_id",
            "run_id",
            "available_at",
            "claimed_by",
            "claimed_until",
            "claim_count",
            "created_at",
        }
    ),
    "artifacts": frozenset(
        {
            "artifact_id",
            "job_id",
            "principal_id",
            "chat_context_id",
            "role",
            "audience",
            "mime",
            "rel_path",
            "size_bytes",
            "content_hash",
            "storage_uri",
            "probe",
            "scan_verdict",
            "validator_version",
            "published_at",
            "created_at",
        }
    ),
}


def test_migration_one_creates_exactly_six_tables() -> None:
    """docs/demo.md picks six of the eleven. A seventh is a scope change, not a refactor."""
    assert set(TABLES) == set(EXPECTED_COLUMNS)


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_table_columns_are_exactly_what_the_docs_name(table: TableName) -> None:
    assert set(TABLES[table]) == EXPECTED_COLUMNS[table]


def test_create_table_statement_count_matches_the_parsed_tables() -> None:
    """Guards the parser itself: a table the regex missed would pass every set check above."""
    created = [
        statement for statement in _statements(SCHEMA) if statement.upper().startswith("CREATE TAB")
    ]
    assert len(created) == len(TABLES) == 6


# --------------------------------------------------------------------------- what is absent


def test_no_idempotency_columns_survive_the_scope_override() -> None:
    """The override supersedes D055 and D089: no replay protection, so no key to store."""
    for table, columns in TABLES.items():
        assert "idempotency_key" not in columns, table
        assert "request_digest" not in columns, table


@pytest.mark.parametrize(
    "table",
    [
        "idempotency_keys",
        "chat_context_grants",
        "workflow_runs",
        "step_records",
        "job_events",
        "cost_entries",
    ],
)
def test_deferred_tables_are_not_created(table: TableName) -> None:
    """D093 keeps their frozen DDL and creates none of them. Creating one here would make the
    demo look like it has an event feed, a checkpoint, or a ledger that nothing writes."""
    assert table not in TABLES


# --------------------------------------------------------------------------- referential shape


def test_every_foreign_key_target_exists_in_this_file() -> None:
    """A migration that references a table it does not create fails at apply time, in a
    container, on somebody's first `make up`. It should fail here instead."""
    targets = set(_REFERENCES_RE.findall(_COMMENT_RE.sub("", SCHEMA)))
    assert targets
    assert targets <= set(TABLES)


def test_jobs_and_artifacts_hang_off_the_request_spine() -> None:
    assert "REFERENCES requests" in TABLES["jobs"]["request_key"]
    assert "REFERENCES jobs" in TABLES["artifacts"]["job_id"]
    assert "REFERENCES jobs" in TABLES["work_items"]["job_id"]


# --------------------------------------------------------------------------- amendments


@pytest.mark.parametrize("table", ["jobs", "artifacts"])
def test_chat_context_id_is_nullable(table: TableName) -> None:
    """A5/D087. A job submitted straight at the API has no conversation, and null is the honest
    value for that. A NOT NULL here means a fake context id in every curl in the demo script."""
    assert "NOT NULL" not in TABLES[table]["chat_context_id"].upper()


def test_artifacts_carry_their_principal() -> None:
    """A6/D088. Without it, listing a learner's artifacts needs a join through `jobs` and gets
    the wrong answer the moment a chat context is absent."""
    assert TABLES["artifacts"]["principal_id"].upper().startswith("TEXT NOT NULL")


def test_artifact_role_audience_and_rel_path_replaced_kind() -> None:
    """A1 to A3. `kind` folded what a file is for, what format it is, and who may see it into
    one enum, and the third question is the one that keeps an agent log away from a learner."""
    artifacts = TABLES["artifacts"]
    assert "kind" not in artifacts
    assert {"role", "audience", "rel_path"} <= set(artifacts)


def test_every_timestamp_column_carries_a_zone() -> None:
    """D072: no local time anywhere. A bare `timestamp` reads back in whatever the session's
    zone happens to be, which is a bug that only shows up on a machine in another country."""
    for table, columns in TABLES.items():
        for column, definition in columns.items():
            if column.endswith(("_at", "_until")):
                assert definition.upper().startswith("TIMESTAMPTZ"), f"{table}.{column}"


# --------------------------------------------------------------------------- indexes

EXPECTED_INDEXES: Final[dict[IndexName, TableName]] = {
    "jobs_by_principal": "jobs",
    "jobs_active": "jobs",
    "jobs_by_request_key": "jobs",
    "work_items_claimable": "work_items",
    "artifacts_by_principal": "artifacts",
    "artifacts_by_job": "artifacts",
    "artifacts_by_hash": "artifacts",
}


@pytest.mark.parametrize(("index", "table"), sorted(EXPECTED_INDEXES.items()))
def test_expected_index_exists_on_its_table(index: IndexName, table: TableName) -> None:
    assert INDEXES.get(index) == table


def test_no_index_targets_a_table_this_migration_does_not_create() -> None:
    assert set(INDEXES.values()) <= set(TABLES)


def test_the_learner_listing_index_carries_its_two_predicates() -> None:
    """A6's index is access control expressed as a plan (docs/demo.md). Drop either predicate
    and the listing can reach a quarantined file or an operator-audience one."""
    statement = next(
        part
        for part in _statements(SCHEMA)
        if part.startswith("CREATE INDEX artifacts_by_principal")
    )
    assert "scan_verdict = 'CLEAN'" in statement
    assert "audience = 'LEARNER'" in statement


def test_the_queue_index_only_covers_unclaimed_rows() -> None:
    """The claim query is `available_at <= now() AND claimed_by IS NULL`. A full index over
    `available_at` makes a busy queue scan every claimed row to find the free one."""
    statement = next(
        part for part in _statements(SCHEMA) if part.startswith("CREATE INDEX work_items_claimable")
    )
    assert "WHERE claimed_by IS NULL" in statement


# --------------------------------------------------------------------------- the connection path


def test_migration_registry_names_a_file_that_exists() -> None:
    """`migrate` reads its SQL by name at runtime, inside a container, after the image is
    built. A missing file is a crash on `make up`, not an import error here."""
    assert [migration.version for migration in MIGRATIONS] == ["0001"]
    for migration in MIGRATIONS:
        assert migration.read().startswith("-- Migration ")


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("postgresql+psycopg://app:app@db:5432/app", "postgresql://app:app@db:5432/app"),
        ("postgresql+asyncpg://app@db/app", "postgresql://app@db/app"),
        ("postgresql://app:app@db:5432/app", "postgresql://app:app@db:5432/app"),
        ("postgres://app@db/app", "postgres://app@db/app"),
        ("dbname=app user=app", "dbname=app user=app"),
    ],
)
def test_sqlalchemy_driver_token_is_stripped(configured: str, expected: str) -> None:
    """`APP_DATABASE_URL` is spelled the SQLAlchemy way in compose. libpq does not know what
    `+psycopg` means and refuses the whole URL, which reads as an unreachable database."""
    assert to_psycopg_dsn(configured) == expected


def test_password_is_not_rewritten_when_it_contains_the_driver_separator() -> None:
    """Only the scheme is touched. A `+` in a password is a legal character and moving it
    changes the credential."""
    dsn = "postgresql+psycopg://app:pa+ss@db:5432/app"
    assert to_psycopg_dsn(dsn) == "postgresql://app:pa+ss@db:5432/app"


def test_probe_satisfies_the_port_the_health_route_asks_for() -> None:
    """`GET /health` is wired to `app.api.ports.DatabaseProbe`. Structural typing means nothing
    fails at import when the shape drifts; it fails at the first request instead."""
    probe = SqlDatabaseProbe(SqlEngine(dsn="postgresql://app@db/app"))
    assert isinstance(probe, DatabaseProbe)
    # `isinstance` on a Protocol checks the name and stops there. The router awaits the result,
    # so a synchronous `ping` would satisfy the check above and return a coroutine-shaped truth.
    assert inspect.iscoroutinefunction(probe.ping)
