"""`GET /v1/artifacts` and `GET /v1/artifacts/{id}/content`.

Steps 4 and 5 of the success test in `docs/demo.md`. One listing endpoint covers "everything I
have made" and "what did this job produce", and one content endpoint is the only path from a
stored object to a client.
"""

from api_fakes import FakeArtifactService, make_artifact
from fastapi.testclient import TestClient

from app.domain.errors import DomainError, ErrorCode
from app.domain.records import Cursor

MISSING_ARTIFACT: str = "art_" + "0" * 26

# --------------------------------------------------------------------------- list


def test_the_list_body_is_an_object_not_an_array(client: TestClient) -> None:
    body = client.get("/v1/artifacts").json()
    assert isinstance(body, dict)
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_listing_returns_summaries(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    artifact = artifact_service.load(make_artifact())
    item = client.get("/v1/artifacts").json()["items"][0]
    assert item["artifact_id"] == artifact.artifact_id
    assert item["content_url"] == f"/v1/artifacts/{artifact.artifact_id}/content"


def test_the_job_id_filter_reaches_the_service(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    artifact = artifact_service.load(make_artifact())
    client.get("/v1/artifacts", params={"job_id": artifact.job_id})
    job_id, _, _ = artifact_service.listed[0]
    assert job_id == artifact.job_id


def test_without_the_filter_the_service_is_asked_for_everything(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    client.get("/v1/artifacts")
    job_id, _, _ = artifact_service.listed[0]
    assert job_id is None


def test_a_malformed_job_id_filter_is_400(client: TestClient) -> None:
    response = client.get("/v1/artifacts", params={"job_id": "nope"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_the_cursor_reaches_the_service(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    artifact = make_artifact()
    cursor = Cursor(created_at=artifact.created_at, id=artifact.artifact_id)
    client.get("/v1/artifacts", params={"cursor": cursor.encode()})
    _, seen, _ = artifact_service.listed[0]
    assert seen == cursor


def test_the_limit_bound_is_enforced(client: TestClient) -> None:
    from app.config import settings

    assert client.get("/v1/artifacts", params={"limit": 0}).status_code == 400
    assert (
        client.get("/v1/artifacts", params={"limit": settings.page_max_limit + 1}).status_code
        == 400
    )


def test_the_next_cursor_is_echoed(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    artifact_service.next_cursor = "opaque"
    assert client.get("/v1/artifacts").json()["next_cursor"] == "opaque"


def test_another_users_artifacts_are_listed_too(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    # @audit no ownership filter. The scope is passed and never applied, so this listing is
    # every artifact in the database. Restoring the check is a WHERE clause in the repository.
    artifact_service.load(make_artifact(principal_id="u_alice"))
    body = client.get("/v1/artifacts", headers={"X-User-Id": "u_bob"}).json()
    assert len(body["items"]) == 1


# --------------------------------------------------------------------------- content


def test_the_bytes_come_back_whole(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    from api_fakes import SAMPLE_BYTES

    artifact = artifact_service.load(make_artifact())
    response = client.get(f"/v1/artifacts/{artifact.artifact_id}/content")
    assert response.status_code == 200
    assert response.content == b"".join(SAMPLE_BYTES)


def test_the_content_headers_are_the_documented_ones(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    from api_fakes import FIXED_CONTENT_HASH, SAMPLE_SIZE

    artifact = artifact_service.load(make_artifact())
    headers = client.get(f"/v1/artifacts/{artifact.artifact_id}/content").headers
    assert headers["Content-Type"] == "video/mp4"
    assert headers["Content-Length"] == str(SAMPLE_SIZE)
    assert headers["ETag"] == f'"{FIXED_CONTENT_HASH}"'
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Disposition"] == 'inline; filename="lesson.mp4"'


def test_the_disposition_comes_from_the_record(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    from api_fakes import make_stream

    # An HTML profile served before an isolated origin exists downgrades to attachment (D081),
    # and that decision belongs to custody's serving policy, not to this router.
    artifact = artifact_service.load(make_artifact())
    artifact_service.stream = make_stream(disposition="attachment")
    headers = client.get(f"/v1/artifacts/{artifact.artifact_id}/content").headers
    assert headers["Content-Disposition"].startswith("attachment;")


def test_a_hostile_filename_cannot_forge_a_header(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    from api_fakes import make_stream

    artifact = artifact_service.load(make_artifact())
    artifact_service.stream = make_stream(filename='ev"il\r\nX-Injected: 1.mp4')
    headers = client.get(f"/v1/artifacts/{artifact.artifact_id}/content").headers
    assert "X-Injected" not in headers
    assert '"' not in headers["Content-Disposition"].split("filename=")[1].strip('"')


def test_an_unknown_artifact_is_404(client: TestClient) -> None:
    response = client.get(f"/v1/artifacts/{MISSING_ARTIFACT}/content")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_a_row_with_no_bytes_is_409_not_ready(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    # Never 404: the artifact exists, so claiming it does not would be a lie the client acts on.
    artifact = artifact_service.load(make_artifact())
    artifact_service.content_error = DomainError(ErrorCode.ARTIFACT_NOT_READY)
    response = client.get(f"/v1/artifacts/{artifact.artifact_id}/content")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ARTIFACT_NOT_READY"


def test_a_malformed_artifact_id_is_400(client: TestClient) -> None:
    assert client.get("/v1/artifacts/nope/content").status_code == 400


def test_another_users_bytes_are_served(
    client: TestClient, artifact_service: FakeArtifactService
) -> None:
    # @audit the download path has no ownership check either. Any caller with an artifact id
    # gets the file. Ids are unguessable uuid5 values, which is obscurity, not authorisation.
    artifact = artifact_service.load(make_artifact(principal_id="u_alice"))
    response = client.get(
        f"/v1/artifacts/{artifact.artifact_id}/content", headers={"X-User-Id": "u_bob"}
    )
    assert response.status_code == 200
