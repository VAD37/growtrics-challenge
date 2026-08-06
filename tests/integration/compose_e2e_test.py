"""The same walkthrough as `pipeline_test.py`, against the stack `make up` starts.

`pipeline_test.py` proves the system; this proves the deployment. It talks HTTP to a published
port and to nothing else: no `app` import, no adapter, no settings object, no database
connection. Everything it knows about the service it learned from a response body, which is what
makes it able to disagree with the in-process run. A Postgres column that never got written, a
MinIO bucket that was never created, a container that starts with the wrong environment -- none
of those can fail the memory-backed suite and all of them fail this one.

The one file it reads from disk is a committed lesson video, and only to compare bytes. A
download that matched "some mp4 of about the right size" would pass against a service that
served the wrong file, so the assertion is the whole content against
`src/app/generation/backends/fixtures/covalent_bonds.mp4` or one of the two lessons beside
it, found by path.

**Skipped by default.** `pytest.ini_options.addopts` carries `-m 'not docker'`, so
`uv run pytest` on a checkout with nothing running never reaches this file. `uv run pytest -m
docker` selects it, and a stack that is not up is still a skip rather than a failure: the API is
probed once per session and the reason names the address that did not answer.

It is slow on purpose. The mock backend renders for 10 to 60 seconds under compose's defaults
(`APP_MOCK_DELAY_MIN_SECONDS`), one job at a time, so a poll deadline here is minutes rather
than seconds. `APP_E2E_TIMEOUT_SECONDS` shortens it when the delay has been turned down.

Each test uses a caller id of its own. Admission counts active jobs per principal (D090), so
sharing one id would make the `429` test depend on whether the worker had drained the previous
test's queue yet.
"""

import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx
import pytest

pytestmark = pytest.mark.docker

BASE_URL_VARIABLE: Final[str] = "APP_E2E_BASE_URL"
DEFAULT_BASE_URL: Final[str] = "http://localhost:8000"
"""Compose's published API port. `make up`, and this address is live."""

TIMEOUT_VARIABLE: Final[str] = "APP_E2E_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 300.0
POLL_SECONDS: Final[float] = 2.0
CONNECT_TIMEOUT_SECONDS: Final[float] = 3.0
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

INSTRUCTION: Final[str] = "why do atoms form covalent bonds"
CONTEXT: Final[list[dict[str, str]]] = [{"kind": "LEVEL", "text": "grade 9"}]
FAIL_TOKEN: Final[str] = "FAIL_ME"
"""The mock backend's one trigger. Spelled out rather than imported: this file imports no `app`."""

MAX_ACTIVE_JOBS: Final[int] = 3
"""`APP_ADMISSION_MAX_ACTIVE_JOBS`, compose's default (D090)."""

TERMINAL: Final[frozenset[str]] = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

FIXTURE_ROOT: Final[Path] = (
    Path(__file__).resolve().parents[2] / "src" / "app" / "generation" / "backends" / "fixtures"
)
LESSON_FILES: Final[tuple[str, ...]] = (
    "covalent_bonds.mp4",
    "ionic_vs_covalent.mp4",
    "ph_scale.mp4",
)


def _base_url() -> str:
    return (os.environ.get(BASE_URL_VARIABLE) or DEFAULT_BASE_URL).rstrip("/")


def _deadline_seconds() -> float:
    return float(os.environ.get(TIMEOUT_VARIABLE) or DEFAULT_TIMEOUT_SECONDS)


def _lesson_digests() -> dict[str, str]:
    """`{sha256: filename}` for the two committed lessons, so a match can be named."""
    return {
        hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest(): name
        for name in LESSON_FILES
    }


@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    """A client against the running API, or a skip naming the address that did not answer.

    `/health` rather than a bare connect, because a container that is up and cannot reach
    Postgres would fail every test below for a reason none of them is about.
    """
    base_url = _base_url()
    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            probe = client.get("/health", timeout=CONNECT_TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            pytest.skip(f"no API at {base_url} (is `make up` finished?): {error}")
        if probe.status_code != 200:
            pytest.skip(f"the API at {base_url} answered /health with {probe.status_code}")
        yield client


def _submit(api: httpx.Client, *, user: str, instruction: str = INSTRUCTION) -> httpx.Response:
    return api.post(
        "/v1/jobs",
        headers={"X-User-Id": user},
        json={"instruction": instruction, "context": CONTEXT},
    )


def _get(api: httpx.Client, path: str, *, user: str) -> httpx.Response:
    return api.get(path, headers={"X-User-Id": user})


def _poll_until_terminal(api: httpx.Client, job_id: str, *, user: str) -> dict[str, object]:
    """Read the job until it stops moving, or fail saying where it stopped.

    Every read is asserted `200` on the way past: a job that becomes unreadable half way through
    is a different failure from a job that never finished, and a poll loop that swallowed the
    status code would report the second for the first.
    """
    deadline = time.monotonic() + _deadline_seconds()
    document: dict[str, object] = {}
    while time.monotonic() < deadline:
        read = _get(api, f"/v1/jobs/{job_id}", user=user)
        assert read.status_code == 200, read.text
        document = read.json()
        if document["status"] in TERMINAL:
            return document
        time.sleep(POLL_SECONDS)
    pytest.fail(
        f"job {job_id} was still {document.get('status')} at {document.get('stage')} "
        f"after {_deadline_seconds()}s"
    )


# --------------------------------------------------------------------------- the walkthrough


def test_a_submitted_job_becomes_a_downloadable_video(api: httpx.Client) -> None:
    """`docs/demo.md`'s five steps, over the wire, against Postgres and MinIO."""
    user = "u_e2e_walkthrough"

    # 1, 2. POST /v1/jobs
    submitted = _submit(api, user=user)
    assert submitted.status_code == 202, submitted.text
    accepted = submitted.json()
    job_id = accepted["job_id"]
    assert job_id.startswith("job_")
    assert accepted["status"] == "QUEUED"
    assert accepted["artifact"] is None

    # 3. Poll until the worker has taken it all the way.
    done = _poll_until_terminal(api, job_id, user=user)
    assert done["status"] == "SUCCEEDED", done
    assert done["stage"] == "DONE"
    assert done["progress"]["percent"] == 100
    assert done["failure"] is None
    artifact = done["artifact"]
    assert artifact is not None
    assert artifact["role"] == "PRIMARY"
    assert artifact["media_type"] == "video/mp4"

    listed_jobs = _get(api, "/v1/jobs", user=user)
    assert listed_jobs.status_code == 200
    assert job_id in {item["job_id"] for item in listed_jobs.json()["items"]}

    # 4. What that job produced: learner rows only, never the operator log.
    listed = _get(api, f"/v1/artifacts?job_id={job_id}", user=user)
    assert listed.status_code == 200
    items = listed.json()["items"]
    roles = [item["role"] for item in items]
    assert "PRIMARY" in roles
    assert "LOG" not in roles
    assert artifact["artifact_id"] in {item["artifact_id"] for item in items}

    # 5. The bytes, whole, against the file on disk.
    content = _get(api, f"/v1/artifacts/{artifact['artifact_id']}/content", user=user)
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("video/mp4")

    digests = _lesson_digests()
    served = hashlib.sha256(content.content).hexdigest()
    assert served in digests, (
        f"the downloaded {len(content.content)} bytes match neither committed lesson"
    )
    assert content.content == (FIXTURE_ROOT / digests[served]).read_bytes()
    assert content.headers["etag"] == f'"sha256:{served}"'


# --------------------------------------------------------------------------- the edges


def test_a_failed_generation_is_a_two_hundred(api: httpx.Client) -> None:
    """`FAIL_ME` reaches the backend through intake and comes back as a readable failure."""
    user = "u_e2e_failure"

    submitted = _submit(api, user=user, instruction=f"{INSTRUCTION} {FAIL_TOKEN}")
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    failed = _poll_until_terminal(api, job_id, user=user)
    assert failed["status"] == "FAILED", failed
    assert failed["stage"] == "FAILED"
    assert failed["artifact"] is None
    assert failed["failure"]["code"] == "GENERATION_FAILED"

    listed = _get(api, f"/v1/artifacts?job_id={job_id}", user=user)
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_a_second_user_reads_the_first_users_job(api: httpx.Client) -> None:
    """The same D111 pair `pipeline_test.py` pins, checked against the running service.

    `QueryJob` takes an `AccessScope` and does not consult it, so the job id is the credential
    and an unknown id is still `404`. Asserted here as well, because the point of running the
    walkthrough twice is that the two runs agree about what the system does.
    """
    submitted = _submit(api, user="u_e2e_owner")
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]

    seen = _get(api, f"/v1/jobs/{job_id}", user="u_e2e_stranger")
    assert seen.status_code == 200
    assert seen.json()["job_id"] == job_id

    missing = _get(api, "/v1/jobs/job_00000000000000000000000000", user="u_e2e_stranger")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_a_fourth_concurrent_submit_is_refused(api: httpx.Client) -> None:
    """Three in flight is the ceiling, counted per principal before anything is inserted.

    Last in the file on purpose: it leaves three jobs for the worker to chew through, and every
    test above it would then be waiting behind them.
    """
    user = "u_e2e_admission"

    for _ in range(MAX_ACTIVE_JOBS):
        accepted = _submit(api, user=user)
        assert accepted.status_code == 202, accepted.text

    refused = _submit(api, user=user)
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "TOO_MANY_ACTIVE_JOBS"
