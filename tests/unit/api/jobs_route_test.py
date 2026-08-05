"""`POST /v1/jobs`, `GET /v1/jobs/{id}`, `GET /v1/jobs`.

Steps 1, 2 and 3 of the success test in `docs/demo.md`. The service behind the seam is a fake,
so what is asserted here is the edge's own behaviour: what it validates, what it hands across,
what it sets on the response, and what it refuses.
"""

from dataclasses import replace

from api_fakes import (
    FakeArtifactService,
    FakeJobService,
    make_artifact,
    make_job,
)
from fastapi.testclient import TestClient

from app.domain.enums import JobStatus, StageName
from app.domain.records import Cursor

# --------------------------------------------------------------------------- submit


def test_submit_answers_202_with_a_fresh_job(
    client: TestClient, valid_body: dict[str, object]
) -> None:
    response = client.post("/v1/jobs", json=valid_body)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["job_id"].startswith("job_")


def test_submit_sets_location_to_the_job(client: TestClient, valid_body: dict[str, object]) -> None:
    response = client.post("/v1/jobs", json=valid_body)
    assert response.headers["Location"] == response.json()["links"]["self"]


def test_two_identical_submits_are_two_jobs(
    client: TestClient, valid_body: dict[str, object]
) -> None:
    # Scope override item 2: no idempotency key, no replay protection, no 409.
    first = client.post("/v1/jobs", json=valid_body).json()
    second = client.post("/v1/jobs", json=valid_body).json()
    assert first["job_id"] != second["job_id"]
    assert first["request_key"] != second["request_key"]


def test_an_idempotency_key_header_is_ignored_rather_than_honoured(
    client: TestClient, valid_body: dict[str, object]
) -> None:
    # It is not read and not validated. A client that sends one gets two jobs anyway.
    first = client.post("/v1/jobs", json=valid_body, headers={"Idempotency-Key": "demo-001"})
    second = client.post("/v1/jobs", json=valid_body, headers={"Idempotency-Key": "demo-001"})
    assert first.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]


def test_the_command_reaches_the_service_with_the_scope(
    client: TestClient, job_service: FakeJobService, valid_body: dict[str, object]
) -> None:
    client.post("/v1/jobs", json=valid_body, headers={"X-User-Id": "u_alice"})
    scope, command, _ = job_service.submitted[0]
    assert scope.principal_id == "u_alice"
    assert command.instruction == valid_body["instruction"]


def test_the_raw_body_is_passed_through_exactly_as_it_arrived(
    client: TestClient, job_service: FakeJobService
) -> None:
    # Scope override item 4: `requests` holds what they typed, `briefs` holds what we asked
    # for. The API touches no table, so the body travels with the command.
    body = {"instruction": "  spaced out  ", "context": [{"kind": "NOTE", "text": "n"}]}
    client.post("/v1/jobs", json=body)
    _, command, raw = job_service.submitted[0]
    assert raw == body
    assert command.instruction == "spaced out"


def test_the_body_the_service_receives_is_a_mapping(
    client: TestClient, job_service: FakeJobService, valid_body: dict[str, object]
) -> None:
    client.post("/v1/jobs", json=valid_body)
    _, _, raw = job_service.submitted[0]
    assert isinstance(raw, dict)


def test_a_json_array_body_is_rejected(client: TestClient) -> None:
    assert client.post("/v1/jobs", json=[{"instruction": "x"}]).status_code == 400


def test_an_invalid_body_never_reaches_the_service(
    client: TestClient, job_service: FakeJobService
) -> None:
    client.post("/v1/jobs", json={"instruction": ""})
    assert job_service.submitted == []


# --------------------------------------------------------------------------- read one


def test_reading_a_job_answers_the_document(
    client: TestClient, job_service: FakeJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING))
    body = client.get(f"/v1/jobs/{job.job_id}").json()
    assert body["job_id"] == job.job_id
    assert body["progress"]["percent"] == 60


def test_an_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/v1/jobs/job_" + "0" * 26)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_another_users_job_reads_200_and_returns_the_job(
    client: TestClient, job_service: FakeJobService
) -> None:
    """@audit THIS IS THE MISSING AUTHORISATION, WRITTEN AS A TEST.

    A real API answers 404 here, and `docs/demo.md` stage D1 says so. The scope override made
    this a free demo with no authorisation at all, so user B reads user A's job and gets it.
    Restoring the check turns this 200 into a JOB_NOT_FOUND and deletes this test.
    """
    job = job_service.load(make_job(principal_id="u_alice"))
    response = client.get(f"/v1/jobs/{job.job_id}", headers={"X-User-Id": "u_bob"})
    assert response.status_code == 200
    assert response.json()["job_id"] == job.job_id


def test_a_caller_with_no_header_at_all_still_reads_the_job(
    client: TestClient, job_service: FakeJobService
) -> None:
    # @audit no credential is required. An absent header becomes the default principal.
    job = job_service.load(make_job(principal_id="u_alice"))
    assert client.get(f"/v1/jobs/{job.job_id}").status_code == 200


def test_a_succeeded_job_carries_its_primary_artifact(
    client: TestClient, job_service: FakeJobService, artifact_service: FakeArtifactService
) -> None:
    artifact = artifact_service.load(make_artifact())
    job = job_service.load(
        make_job(
            status=JobStatus.SUCCEEDED,
            stage=StageName.DONE,
            artifact_id=artifact.artifact_id,
        )
    )
    body = client.get(f"/v1/jobs/{job.job_id}").json()
    assert body["artifact"]["artifact_id"] == artifact.artifact_id
    assert body["artifact"]["content_url"].endswith("/content")


def test_a_queued_job_costs_no_artifact_lookup(
    client: TestClient, job_service: FakeJobService, artifact_service: FakeArtifactService
) -> None:
    job = job_service.load(make_job())
    client.get(f"/v1/jobs/{job.job_id}")
    assert artifact_service.listed == []


def test_a_missing_artifact_row_leaves_the_field_null(
    client: TestClient, job_service: FakeJobService
) -> None:
    # The job says it has one and custody has not published it. Null beats a 500.
    job = job_service.load(
        make_job(
            status=JobStatus.SUCCEEDED,
            stage=StageName.DONE,
            artifact_id=make_artifact().artifact_id,
        )
    )
    assert client.get(f"/v1/jobs/{job.job_id}").json()["artifact"] is None


# --------------------------------------------------------------------------- etag


def test_the_etag_is_the_job_version(client: TestClient, job_service: FakeJobService) -> None:
    job = job_service.load(make_job(version=3))
    assert client.get(f"/v1/jobs/{job.job_id}").headers["ETag"] == 'W/"3"'


def test_a_matching_if_none_match_is_304_with_no_body(
    client: TestClient, job_service: FakeJobService
) -> None:
    job = job_service.load(make_job(version=3))
    response = client.get(f"/v1/jobs/{job.job_id}", headers={"If-None-Match": 'W/"3"'})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["ETag"] == 'W/"3"'


def test_a_stale_if_none_match_is_a_full_200(
    client: TestClient, job_service: FakeJobService
) -> None:
    job = job_service.load(make_job(version=4))
    response = client.get(f"/v1/jobs/{job.job_id}", headers={"If-None-Match": 'W/"3"'})
    assert response.status_code == 200


def test_a_strong_tag_matches_the_weak_one_it_was_derived_from(
    client: TestClient, job_service: FakeJobService
) -> None:
    # RFC 9110 weak comparison: W/"3" and "3" are the same entity for If-None-Match.
    job = job_service.load(make_job(version=3))
    assert client.get(f"/v1/jobs/{job.job_id}", headers={"If-None-Match": '"3"'}).status_code == 304


def test_a_list_of_tags_matches_on_any_member(
    client: TestClient, job_service: FakeJobService
) -> None:
    job = job_service.load(make_job(version=3))
    headers = {"If-None-Match": 'W/"1", W/"3"'}
    assert client.get(f"/v1/jobs/{job.job_id}", headers=headers).status_code == 304


def test_a_wildcard_if_none_match_matches(client: TestClient, job_service: FakeJobService) -> None:
    job = job_service.load(make_job(version=3))
    assert client.get(f"/v1/jobs/{job.job_id}", headers={"If-None-Match": "*"}).status_code == 304


def test_a_304_costs_no_artifact_lookup(
    client: TestClient, job_service: FakeJobService, artifact_service: FakeArtifactService
) -> None:
    # The whole point of D084: an unchanged poll costs one read and no body.
    artifact = artifact_service.load(make_artifact())
    job = job_service.load(
        make_job(
            status=JobStatus.SUCCEEDED,
            stage=StageName.DONE,
            version=3,
            artifact_id=artifact.artifact_id,
        )
    )
    client.get(f"/v1/jobs/{job.job_id}", headers={"If-None-Match": 'W/"3"'})
    assert artifact_service.listed == []


# --------------------------------------------------------------------------- list


def test_the_list_body_is_an_object_not_an_array(client: TestClient) -> None:
    body = client.get("/v1/jobs").json()
    assert isinstance(body, dict)
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_the_list_is_newest_first(client: TestClient, job_service: FakeJobService) -> None:
    older = make_job(job_id="job_" + "1" * 26, created_at=make_job().created_at.replace(hour=8))
    newer = make_job()
    job_service.load(older)
    job_service.load(newer)
    items = client.get("/v1/jobs").json()["items"]
    assert [item["job_id"] for item in items] == [newer.job_id, older.job_id]


def test_the_list_passes_the_cursor_through(
    client: TestClient, job_service: FakeJobService
) -> None:
    cursor = Cursor(created_at=make_job().created_at, id=make_job().job_id)
    client.get("/v1/jobs", params={"cursor": cursor.encode()})
    _, seen, _ = job_service.listed[0]
    assert seen == cursor


def test_the_next_cursor_is_echoed_from_the_page(
    client: TestClient, job_service: FakeJobService
) -> None:
    job_service.next_cursor = "opaque"
    assert client.get("/v1/jobs").json()["next_cursor"] == "opaque"


def test_a_malformed_cursor_is_400(client: TestClient) -> None:
    response = client.get("/v1/jobs", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "cursor"


def test_the_default_limit_is_the_configured_one(
    client: TestClient, job_service: FakeJobService
) -> None:
    from app.config import settings

    client.get("/v1/jobs")
    _, _, limit = job_service.listed[0]
    assert limit == settings.page_default_limit


def test_an_over_large_limit_is_rejected_rather_than_clamped(client: TestClient) -> None:
    from app.config import settings

    response = client.get("/v1/jobs", params={"limit": settings.page_max_limit + 1})
    assert response.status_code == 400


def test_a_zero_limit_is_rejected(client: TestClient) -> None:
    assert client.get("/v1/jobs", params={"limit": 0}).status_code == 400


def test_list_items_are_job_documents(client: TestClient, job_service: FakeJobService) -> None:
    job_service.load(make_job())
    item = client.get("/v1/jobs").json()["items"][0]
    assert item["links"]["self"].endswith(item["job_id"])


def test_a_listed_job_carries_no_embedded_artifact(
    client: TestClient, job_service: FakeJobService, artifact_service: FakeArtifactService
) -> None:
    # Deliberate: the seam has no batch artifact read, and enriching a page of jobs one row at
    # a time is the N+1 that would make the list endpoint the slowest call in the demo. The
    # detail endpoint fills it in, and `links.artifacts` is on every item.
    artifact = artifact_service.load(make_artifact())
    job_service.load(
        replace(
            make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE),
            artifact_id=artifact.artifact_id,
        )
    )
    item = client.get("/v1/jobs").json()["items"][0]
    assert item["artifact"] is None
    assert artifact_service.listed == []
