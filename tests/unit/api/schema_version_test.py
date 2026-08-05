"""One constant, everywhere it has to appear.

Three levels of version meet at this edge and they are not the same thing: the path `/v1`
moves only on a breaking change, `schema_version` moves on every additive one, and
`jobs.contract_version` / `artifacts.validator_version` describe the job rather than the wire.
A version string written twice will disagree with itself, so every assertion below compares
against the single constant rather than against a literal.
"""

import re
from typing import Final

from api_fakes import FakeArtifactService, FakeJobService, make_artifact, make_job
from fastapi.testclient import TestClient

from app.api.schemas.common import SCHEMA_VERSION, SCHEMA_VERSION_HEADER, SCHEMA_VERSION_PATTERN

MAJOR_MINOR: Final[re.Pattern[str]] = re.compile(SCHEMA_VERSION_PATTERN)


def test_the_constant_parses_as_major_minor() -> None:
    assert MAJOR_MINOR.fullmatch(SCHEMA_VERSION) is not None


def test_the_header_name_is_the_documented_one() -> None:
    assert SCHEMA_VERSION_HEADER == "X-Schema-Version"


def test_health_carries_the_version_in_the_body_and_the_header(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json()["schema_version"] == SCHEMA_VERSION
    assert response.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION


def test_a_job_document_carries_it(client: TestClient, valid_body: dict[str, object]) -> None:
    response = client.post("/v1/jobs", json=valid_body)
    assert response.json()["schema_version"] == SCHEMA_VERSION
    assert response.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION


def test_both_list_pages_carry_it(client: TestClient) -> None:
    for path in ("/v1/jobs", "/v1/artifacts"):
        body = client.get(path).json()
        assert body["schema_version"] == SCHEMA_VERSION, path


def test_the_error_envelope_carries_it(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"instruction": ""})
    assert response.status_code == 400
    assert response.json()["schema_version"] == SCHEMA_VERSION


def test_the_header_matches_the_body_on_an_error(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"instruction": ""})
    assert response.headers[SCHEMA_VERSION_HEADER] == response.json()["schema_version"]


def test_a_304_still_carries_the_header(client: TestClient, job_service: FakeJobService) -> None:
    # A body-less response is where a header set at serialisation time would be missed.
    job = job_service.load(make_job(version=7))
    first = client.get(f"/v1/jobs/{job.job_id}")
    second = client.get(f"/v1/jobs/{job.job_id}", headers={"If-None-Match": first.headers["ETag"]})
    assert second.status_code == 304
    assert second.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION


def test_a_404_from_an_unrouted_path_still_carries_the_header(client: TestClient) -> None:
    response = client.get("/v1/nothing-here")
    assert response.status_code == 404
    assert response.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION


def test_the_content_response_carries_the_header(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    artifact = artifact_service.load(make_artifact())
    response = client.get(f"/v1/artifacts/{artifact.artifact_id}/content")
    assert response.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION


def test_a_nested_view_does_not_repeat_the_version(
    client: TestClient, valid_body: dict[str, object]
) -> None:
    body = client.post("/v1/jobs", json=valid_body).json()
    assert "schema_version" not in body["progress"]
    assert "schema_version" not in body["links"]
    assert "schema_version" not in body["constraints"]


def test_a_listed_job_is_the_same_document_as_a_fetched_one(
    client: TestClient, job_service: FakeJobService
) -> None:
    # `JobView` is one model with one shape, so a job inside a page carries the version the
    # same way a job on its own does. The alternative is a second job model that differs by
    # one field, and a client that has to know which of the two it is holding.
    job = job_service.load(make_job())
    listed = client.get("/v1/jobs").json()["items"][0]
    fetched = client.get(f"/v1/jobs/{job.job_id}").json()
    assert listed == fetched
    assert listed["schema_version"] == SCHEMA_VERSION
