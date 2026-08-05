"""Where SQL may be written, and what shape it has to be written in.

SQL is the hot path and the one place a typo is a data bug rather than a stack trace, so the
rule is that it lives in one package and every statement has a name. Three things follow from
that, and this file holds all three:

* Nothing outside `app/storage/sql/` contains a SQL statement. A query inlined in a use case is
  a database call nobody can find, and it is how a port stops being a port.
* Nothing outside `app/storage/sql/` imports `psycopg`. `import-linter` says the same thing, and
  it is repeated here because nothing runs `lint-imports` automatically yet (`CLAUDE.md`).
* Every statement inside that package sits in a named function. "One statement, one named
  operation" is what makes a database action greppable: a reviewer asks what writes `jobs` and
  gets a list of function names rather than a list of string literals.

The last one is pinned as an inventory below, so a new database operation costs a line in this
file. That is deliberate. The point of the rule is that adding a way to talk to Postgres is a
decision somebody made on purpose.

Read as text through `ast`, never over a connection. Docstrings and comments are excluded on
purpose: prose that says `FOR UPDATE SKIP LOCKED` is documentation and is wanted, and the ports
are full of it. What this file looks for is a statement a driver could execute.
"""

import ast
import re
from pathlib import Path
from typing import Final

import app

# --------------------------------------------------------------------------- reading

_SOURCE_ROOT: Final[Path] = Path(app.__file__).parent
_SQL_PACKAGE: Final[Path] = _SOURCE_ROOT / "storage" / "sql"

_SQL_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(SELECT\s|INSERT\s+INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b|CREATE\s+(TABLE|INDEX)\b"
    r"|ALTER\s+TABLE\b|DROP\s+TABLE\b)"
)
"""Uppercase and anchored on a verb, so `select` in prose and `update` in a name are not SQL."""

type FunctionName = str
type Statement = str


def python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Every string constant that is a docstring, by object id.

    A docstring is the first statement of a module, class, or function, and it is prose. The SQL
    in `migrate.py`'s module docstring is an explanation of what the file does, not a call.
    """
    marked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            marked.add(id(first.value))
    return marked


def sql_literals(path: Path) -> tuple[tuple[FunctionName, Statement], ...]:
    """Every executable-looking SQL string in a file, with the function it is written inside.

    The enclosing name is `<module>` for a literal at module level, which is legal: a statement
    bound to a named constant is as findable as one inside a function. What the inventory below
    refuses is a statement with no name at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_nodes(tree)
    found: list[tuple[FunctionName, Statement]] = []

    def visit(node: ast.AST, enclosing: FunctionName) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            enclosing = node.name
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _SQL_RE.search(node.value)
        ):
            found.append((enclosing, node.value.strip().splitlines()[0].strip()))
        for child in ast.iter_child_nodes(node):
            visit(child, enclosing)

    visit(tree, "<module>")
    return tuple(found)


def imported_names(path: Path) -> frozenset[str]:
    """Top-level package name of every import in a file: `psycopg.rows` counts as `psycopg`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return frozenset(names)


# --------------------------------------------------------------------------- the rules

DRIVERS: Final[frozenset[str]] = frozenset({"psycopg", "sqlalchemy", "asyncpg"})
"""What speaks to Postgres. Importing one of these is what "a direct database call" means."""


def test_no_sql_is_written_outside_the_sql_package() -> None:
    """A query inlined in a use case is a database call nobody can find.

    It also skips every guard the package holds: the statement is not in `schema.sql`, so the
    schema test does not see it, and it is not behind a repository method, so nothing names it.
    """
    offenders = {
        str(path.relative_to(_SOURCE_ROOT)): [statement for _, statement in literals]
        for path in python_files(_SOURCE_ROOT)
        if not path.is_relative_to(_SQL_PACKAGE)
        for literals in [sql_literals(path)]
        if literals
    }
    assert offenders == {}, f"SQL outside app/storage/sql/: {offenders}"


def test_no_driver_is_imported_outside_the_sql_package() -> None:
    """The port is the seam. A module holding a connection has reached around it.

    `import-linter` enforces this too. It is asserted here as well because `uv run pytest` is the
    only check that runs on its own, and a rule nobody runs is a comment (D065).
    """
    offenders = {
        str(path.relative_to(_SOURCE_ROOT)): sorted(imported_names(path) & DRIVERS)
        for path in python_files(_SOURCE_ROOT)
        if not path.is_relative_to(_SQL_PACKAGE) and imported_names(path) & DRIVERS
    }
    assert offenders == {}, f"database driver imported outside app/storage/sql/: {offenders}"


DATABASE_OPERATIONS: Final[frozenset[FunctionName]] = frozenset(
    {
        "<module>",  # `_BOOKKEEPING_DDL`, the migration bookkeeping table
        "apply_migrations",
        "ping",
    }
)
"""Every function in `app/storage/sql/` that executes SQL, pinned.

Adding one costs a line here, which is the point: a new way to talk to Postgres is a decision,
and this list is where a reviewer reads the whole set of them at once.

@TODO the repositories are not written. When `repositories.py` and `queue.py` land, this set
grows by one name per port method and stops being three entries long.
"""


def test_every_statement_lives_in_a_named_operation() -> None:
    """One statement, one name. An anonymous query is one nobody can audit or find again."""
    written = {name for path in python_files(_SQL_PACKAGE) for name, _ in sql_literals(path)}
    assert written <= DATABASE_OPERATIONS, (
        f"undeclared database operation: {sorted(written - DATABASE_OPERATIONS)}. "
        "Add it to DATABASE_OPERATIONS with a reason, or move the statement into a named method."
    )


def test_the_inventory_names_nothing_that_is_gone() -> None:
    """A stale entry is worse than a missing one: it makes the list look maintained."""
    written = {name for path in python_files(_SQL_PACKAGE) for name, _ in sql_literals(path)}
    assert written >= DATABASE_OPERATIONS, (
        f"DATABASE_OPERATIONS names an operation that no longer exists: "
        f"{sorted(DATABASE_OPERATIONS - written)}"
    )


def test_the_reader_can_tell_sql_from_prose() -> None:
    """The guard above is only worth its runtime if it would actually catch something.

    `migrate.py` has SQL in a docstring and SQL in a call. One is prose and one is a statement,
    and a checker that cannot tell them apart is either noise or asleep.
    """
    migrate = _SQL_PACKAGE / "migrate.py"
    literals = sql_literals(migrate)

    assert any(statement.startswith("SELECT pg_advisory_xact_lock") for _, statement in literals)
    assert all("loser waits" not in statement for _, statement in literals)
