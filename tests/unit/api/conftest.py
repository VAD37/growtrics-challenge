"""Fixtures for the HTTP edge. The fakes themselves live in `api_fakes.py`.

The app under test is built the way `main.py` will build it: `install(app)` attaches the
routers, the exception handlers, and the schema-version header, and every provider in
`deps.py` is overridden with a fake. Nothing here reaches a database, an object store, or
another lane's package.
"""

from collections.abc import Iterator

import pytest
from api_fakes import (
    DEFAULT_PRINCIPAL,
    FakeArtifactService,
    FakeDatabaseProbe,
    FakeJobService,
    FakePrincipalRepository,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.access.stub import DemoPrincipalResolver
from app.api import install
from app.api.deps import (
    get_artifact_service,
    get_database_probe,
    get_job_service,
    get_principal_resolver,
)


@pytest.fixture
def job_service() -> FakeJobService:
    return FakeJobService()


@pytest.fixture
def artifact_service() -> FakeArtifactService:
    return FakeArtifactService()


@pytest.fixture
def database_probe() -> FakeDatabaseProbe:
    return FakeDatabaseProbe()


@pytest.fixture
def api(
    job_service: FakeJobService,
    artifact_service: FakeArtifactService,
    database_probe: FakeDatabaseProbe,
) -> FastAPI:
    resolver = DemoPrincipalResolver(
        FakePrincipalRepository(), default_principal_id=DEFAULT_PRINCIPAL
    )
    app = FastAPI()
    # `event_stream=True` where `main.py` passes the default: the streaming endpoint is cut from
    # the demo and still has to be tested, which is what the flag is for (`app/api/__init__.py`).
    install(app, event_stream=True)
    app.dependency_overrides[get_job_service] = lambda: job_service
    app.dependency_overrides[get_artifact_service] = lambda: artifact_service
    app.dependency_overrides[get_database_probe] = lambda: database_probe
    app.dependency_overrides[get_principal_resolver] = lambda: resolver
    return app


@pytest.fixture
def client(api: FastAPI) -> Iterator[TestClient]:
    with TestClient(api) as test_client:
        yield test_client


@pytest.fixture
def valid_body() -> dict[str, object]:
    """The body from the demo script in `docs/demo.md`."""
    return {
        "instruction": "why do atoms form covalent bonds",
        "context": [{"kind": "LEVEL", "text": "grade 9"}],
    }
