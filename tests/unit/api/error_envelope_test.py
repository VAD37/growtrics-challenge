"""Every failure leaves through one envelope, and its message comes from one catalog.

The mapping is `ERROR_CATALOG` and nothing else (D062). A status written at a raise site and a
message written next to it are the two ways an API starts contradicting its own documentation,
so both are tested against the catalog rather than against literals -- except the eight status
codes themselves, which `docs/demo.md` fixes and which are therefore worth pinning literally.
"""

from typing import Final

import pytest
from api_fakes import FakeJobService, make_job
from fastapi.testclient import TestClient

from app.api.errors import ENVELOPE_KEYS, ERROR_BODY_KEYS, http_status_for
from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode

DEMO_STATUSES: Final[dict[ErrorCode, int]] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.ARTIFACT_NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.ARTIFACT_NOT_READY: 409,
    ErrorCode.TOO_MANY_ACTIVE_JOBS: 429,
}


# --------------------------------------------------------------------------- status mapping


@pytest.mark.parametrize(("code", "status"), sorted(DEMO_STATUSES.items()))
def test_every_request_code_maps_to_the_documented_status(code: ErrorCode, status: int) -> None:
    assert http_status_for(code) == status


def test_a_job_failure_code_at_the_edge_is_a_bug_and_answers_500() -> None:
    # GENERATION_FAILED belongs in `failure.code` on a 200 job document. Reaching the edge as
    # a raised error means a caller found a path nobody designed, and that is a server fault.
    assert ERROR_CATALOG[ErrorCode.GENERATION_FAILED].http_status is None
    assert http_status_for(ErrorCode.GENERATION_FAILED) == 500


def test_every_code_in_the_catalog_has_a_status_at_the_edge() -> None:
    for code in ErrorCode:
        assert 400 <= http_status_for(code) <= 599


# --------------------------------------------------------------------------- envelope shape


def test_the_envelope_shape_is_the_frozen_one(client: TestClient) -> None:
    body = client.get("/v1/jobs/job_" + "0" * 26).json()
    assert set(body) == set(ENVELOPE_KEYS)
    assert set(body["error"]) == set(ERROR_BODY_KEYS)


def test_the_message_is_the_catalog_message(client: TestClient) -> None:
    body = client.get("/v1/jobs/job_" + "0" * 26).json()
    assert body["error"]["code"] == "JOB_NOT_FOUND"
    assert body["error"]["message"] == ERROR_CATALOG[ErrorCode.JOB_NOT_FOUND].message


def test_a_not_found_message_does_not_echo_the_id_that_was_guessed(client: TestClient) -> None:
    # A "no such job job_XYZ" message is an enumeration oracle.
    missing = "job_" + "0" * 26
    body = client.get(f"/v1/jobs/{missing}").json()
    assert missing not in body["error"]["message"]


def test_details_is_always_an_object(client: TestClient) -> None:
    body = client.get("/v1/jobs/job_" + "0" * 26).json()
    assert body["error"]["details"] == {}


# --------------------------------------------------------------------------- validation


def test_a_schema_violation_is_400_invalid_request(client: TestClient) -> None:
    response = client.post("/v1/jobs", json={"instruction": "x" * 501})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_the_default_422_is_never_used(client: TestClient) -> None:
    # FastAPI answers 422 out of the box. The frozen contract says 400 INVALID_REQUEST.
    assert client.post("/v1/jobs", json={}).status_code == 400


def test_details_name_the_field_and_the_bound(client: TestClient) -> None:
    details = client.post(
        "/v1/jobs", json={"instruction": "x", "options": {"max_duration_s": 181}}
    ).json()["error"]["details"]
    assert details["field"].endswith("max_duration_s")
    assert details["limit"] == "180"


def test_details_name_an_unknown_field(client: TestClient) -> None:
    details = client.post("/v1/jobs", json={"instruction": "x", "nope": 1}).json()["error"][
        "details"
    ]
    assert details["field"].endswith("nope")


def test_details_values_are_all_strings(client: TestClient) -> None:
    details = client.post("/v1/jobs", json={"instruction": ""}).json()["error"]["details"]
    assert details
    assert all(isinstance(value, str) for value in details.values())


def test_a_malformed_body_is_400_rather_than_a_stack_trace(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_a_malformed_id_in_the_path_is_400_rather_than_404(client: TestClient) -> None:
    # The id never reaches a service, so "not found" would be a claim nobody checked.
    response = client.get("/v1/jobs/not-an-id")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


# --------------------------------------------------------------------------- transport


def test_an_unrouted_path_still_answers_an_envelope(client: TestClient) -> None:
    body = client.get("/v1/nowhere").json()
    assert set(body) == set(ENVELOPE_KEYS)
    assert body["error"]["code"] == "INVALID_REQUEST"


def test_a_wrong_method_still_answers_an_envelope(client: TestClient) -> None:
    response = client.delete("/v1/jobs")
    assert response.status_code == 405
    assert set(response.json()) == set(ENVELOPE_KEYS)


# --------------------------------------------------------------------------- from a service


def test_a_domain_error_raised_behind_the_seam_becomes_its_status(
    client: TestClient, job_service: FakeJobService, valid_body: dict[str, object]
) -> None:
    job_service.submit_error = DomainError(ErrorCode.TOO_MANY_ACTIVE_JOBS, {"active": "3"})
    response = client.post("/v1/jobs", json=valid_body)
    assert response.status_code == 429
    assert response.json()["error"] == {
        "code": "TOO_MANY_ACTIVE_JOBS",
        "message": ERROR_CATALOG[ErrorCode.TOO_MANY_ACTIVE_JOBS].message,
        "details": {"active": "3"},
    }


def test_admission_is_enforced_behind_the_seam_not_at_the_edge(
    client: TestClient, job_service: FakeJobService, valid_body: dict[str, object]
) -> None:
    # D090 is one indexed count in the same transaction as the insert. The edge cannot run it
    # without touching a table, so all it owns is the mapping from the code to 429.
    for _ in range(4):
        client.post("/v1/jobs", json=valid_body)
    assert len(job_service.submitted) == 4


def test_a_failed_job_is_still_a_200_read(client: TestClient, job_service: FakeJobService) -> None:
    from dataclasses import replace

    from app.domain.enums import JobStatus, StageName
    from app.domain.records import FailureRecord

    failure = FailureRecord(
        code=ErrorCode.GENERATION_FAILED,
        stage=StageName.GENERATING,
        message=ERROR_CATALOG[ErrorCode.GENERATION_FAILED].message,
        retryable=False,
        occurred_at=make_job().updated_at,
        trace_id="tr_" + "0" * 26,
    )
    job = job_service.load(
        replace(make_job(status=JobStatus.FAILED, stage=StageName.FAILED), failure=failure)
    )
    response = client.get(f"/v1/jobs/{job.job_id}")
    assert response.status_code == 200
    assert response.json()["failure"]["code"] == "GENERATION_FAILED"
