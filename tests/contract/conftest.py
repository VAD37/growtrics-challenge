"""One suite, two backends, and the event loop the driver needs on Windows.

Every test in this directory takes a `backend` fixture and is run twice: once against the
in-memory doubles and once against Postgres. Writing the assertions once is the only way the
doubles stay trustworthy, because they are what every other suite in this repository is standing
on (`app/storage/memory/__init__.py`, D049).

The SQL parameter skips when there is no database to reach. `uv run pytest` on a checkout with
no Docker passes with half of each test file marked skipped, and `make up` turns the other half
on with no flag and no environment variable. `APP_TEST_DATABASE_URL` overrides the address when
the database is somewhere other than compose's published port.

**The loop factory.** psycopg's async connection registers a socket reader on the event loop, and
Windows defaults to a `ProactorEventLoop`, which has none: every connect raises `InterfaceError`
naming the loop. pytest-asyncio builds its runner from `pytest_asyncio_loop_factories`, so
implementing that hook here hands this directory a selector loop and leaves the rest of the suite
on the platform default. `asyncio.set_event_loop_policy` would do the same job and is deprecated
for removal in 3.16, which is why it is not what this uses. `app/storage/sql/engine.py` carries
the same note for the entrypoints.
"""

import asyncio
import sys
from collections.abc import Iterator, Mapping
from typing import Final

import pytest
from support.backends import (
    BACKENDS,
    SQL,
    StorageBackend,
    configured_dsn,
    memory_backend,
    prepare_database,
    sql_backend,
    truncate,
)

_SELECTOR_LOOP_NEEDED: Final[bool] = sys.platform == "win32"


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> Mapping[str, object]:
    """The loop pytest-asyncio runs these tests on. See the module docstring."""
    del config, item
    if _SELECTOR_LOOP_NEEDED:
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.EventLoop}


@pytest.fixture(scope="session")
def sql_dsn() -> str:
    return configured_dsn()


@pytest.fixture(scope="session")
def sql_skip_reason(sql_dsn: str) -> str | None:
    """Migrate the test database once per session, or say why the SQL half cannot run."""
    return prepare_database(sql_dsn)


@pytest.fixture(params=BACKENDS)
def backend(
    request: pytest.FixtureRequest, sql_dsn: str, sql_skip_reason: str | None
) -> Iterator[StorageBackend]:
    """One backend per parameter, emptied before the test rather than after.

    Before, because a test that fails is a test whose rows are worth looking at, and a teardown
    that tidied them away would delete the evidence at exactly the moment it mattered.
    """
    if request.param != SQL:
        yield memory_backend()
        return
    if sql_skip_reason is not None:
        pytest.skip(sql_skip_reason)
    truncate(sql_dsn)
    yield sql_backend(sql_dsn)
