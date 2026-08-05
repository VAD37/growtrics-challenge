"""`GET /health`: is the database reachable.

Liveness alone answers "the process is up", which is the question nobody asks during a demo.
The probe is injected, so this endpoint is testable without a database and swappable without
touching the router.
"""

from api_fakes import FakeDatabaseProbe
from fastapi.testclient import TestClient

from app.api.schemas.common import SCHEMA_VERSION


def test_a_reachable_database_is_200_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "database": "up",
        "version": response.json()["version"],
    }


def test_an_unreachable_database_is_503_degraded(
    client: TestClient, database_probe: FakeDatabaseProbe
) -> None:
    database_probe.reachable = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "down"


def test_a_probe_that_raises_is_a_down_database_rather_than_a_500(
    client: TestClient, database_probe: FakeDatabaseProbe
) -> None:
    # A connection error is the answer to the question, not an accident.
    database_probe.raises = RuntimeError("connection refused")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == "down"


def test_health_reports_the_running_version(client: TestClient) -> None:
    from app.config import settings

    assert client.get("/health").json()["version"] == settings.version


def test_health_needs_no_identity(client: TestClient) -> None:
    # It is outside /v1 and outside the scope model: no principal is resolved to answer it.
    assert client.get("/health").status_code == 200
