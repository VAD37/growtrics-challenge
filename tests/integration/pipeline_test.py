"""The demo, proved in one process: submit over HTTP, run the worker, get the video back.

This is `docs/demo.md`'s five-step success test written down as a test, and its first obligation
is that it runs on a clean checkout with no Docker and no Postgres. So the storage is the memory
doubles from `app/storage/memory/`, which the contract suite in `tests/contract/` keeps honest
against the real adapters, and everything above them is the shipped system: the composition root
wires it, the FastAPI app is `build_app`'s, the worker loop is `app/worker.py`'s, and the runner,
the steps, the ACL, custody and the mock backend are the ones the container runs.

Only two things differ from `make up`, and both are named here rather than hidden in a fixture:

* Storage is in memory rather than Postgres and MinIO. `tests/integration/compose_e2e_test.py`
  is the same walkthrough against the real ones.
* The mock backend's delay is zero. It is 10 to 60 seconds in production so a reviewer can watch
  a job move (`config.mock_delay_min_seconds`); a suite that waited for it would be a suite
  nobody runs.

The worker turns by hand, one `claim_and_run_once` at a time, rather than as a background task.
A test that spawned the claim loop would be a test whose failures are timing, and the loop's own
behaviour -- polling, heartbeats, shutdown -- is already covered by
`tests/unit/orchestration/worker_test.py`.

`Adapters` is built here with the doubles in the fields its annotations name after the SQL and S3
classes. That is deliberate and it is the whole point: the wiring functions in `app/composition.py`
name ports, so handing them a double proves they never reached for a driver.
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import httpx
import pytest

from app.composition import (
    Adapters,
    SystemClock,
    build_runner,
    close_resources,
    open_resources,
)
from app.config import settings
from app.domain.enums import ArtifactRole, Audience
from app.generation.backends.mock import (
    FAIL_TOKEN,
    LESSON_VIDEOS,
    MockGenerationBackend,
    video_for_brief,
)
from app.main import build_app
from app.storage.memory import (
    MemoryArtifactRepository,
    MemoryBriefRepository,
    MemoryDatabase,
    MemoryDatabaseProbe,
    MemoryJobRepository,
    MemoryObjectStore,
    MemoryPrincipalRepository,
    MemoryRequestStore,
    MemoryUnitOfWork,
    MemoryWorkQueue,
)
from app.worker import WorkerConfig, WorkerLoop

USER: Final[str] = "u_demo"
OTHER_USER: Final[str] = "u_someone_else"
BASE_URL: Final[str] = "http://demo.invalid"

INSTRUCTION: Final[str] = "why do atoms form covalent bonds"
CONTEXT: Final[list[dict[str, str]]] = [{"kind": "LEVEL", "text": "grade 9"}]

LEASE_SECONDS: Final[int] = 60
OWNER: Final[str] = "pipeline-test"

UNKNOWN_JOB_ID: Final[str] = "job_00000000000000000000000000"


class _NoConnectionsEngine:
    """`SqlEngine`'s lifetime, without the pool.

    `open_resources` and `close_resources` are called for real so the entrypoint's start-up
    sequence is exercised; there is simply nothing to connect to. The doubles hold their own
    state and need no engine, which is why nothing else on this object is ever reached.
    """

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _BucketedObjectStore(MemoryObjectStore):
    """`MemoryObjectStore` plus the two members the composition root's start-up touches.

    `ensure_bucket` is `S3ObjectStore`'s idempotent head-and-create (D096). In a dictionary the
    bucket is always there, so this is the honest no-op rather than a stub with an opinion.
    """

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Harness:
    """One wired system: the HTTP client in front of it and the worker behind it."""

    adapters: Adapters
    database: MemoryDatabase
    objects: _BucketedObjectStore
    client: httpx.AsyncClient
    worker: WorkerLoop

    async def submit(
        self,
        *,
        instruction: str = INSTRUCTION,
        user: str = USER,
    ) -> httpx.Response:
        return await self.client.post(
            "/v1/jobs",
            headers={"X-User-Id": user},
            json={"instruction": instruction, "context": CONTEXT},
        )

    async def get(self, path: str, *, user: str = USER) -> httpx.Response:
        return await self.client.get(path, headers={"X-User-Id": user})

    async def turn(self) -> bool:
        """One claim-run-complete turn of the worker loop. `False` when the queue was empty."""
        return await self.worker.claim_and_run_once()


def _memory_adapters() -> tuple[Adapters, MemoryDatabase, _BucketedObjectStore]:
    """Every port the demo needs, over the doubles, with the mock backend's delay at zero."""
    database = MemoryDatabase()
    clock = SystemClock()
    objects = _BucketedObjectStore(bucket=settings.object_store_bucket)
    adapters = Adapters(
        settings=settings,
        clock=clock,
        engine=_NoConnectionsEngine(),
        objects=objects,
        backend=MockGenerationBackend(delay_min_seconds=0.0, delay_max_seconds=0.0),
        principals=MemoryPrincipalRepository(database, clock),
        requests=MemoryRequestStore(database),
        briefs=MemoryBriefRepository(database),
        jobs=MemoryJobRepository(database, clock),
        artifacts=MemoryArtifactRepository(database),
        unit_of_work=MemoryUnitOfWork(database),
        queue=MemoryWorkQueue(database, clock),
        probe=MemoryDatabaseProbe(database),
    )
    return adapters, database, objects


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    """A whole system per test, opened and closed the way the entrypoints do it."""
    adapters, database, objects = _memory_adapters()
    await open_resources(adapters)

    app = build_app(adapters)
    worker = WorkerLoop(
        queue=adapters.queue,
        runner=build_runner(adapters),
        clock=adapters.clock,
        config=WorkerConfig(
            owner=OWNER,
            lease_seconds=LEASE_SECONDS,
            poll_seconds=0.0,
            max_concurrent_jobs=1,
        ),
        stop=asyncio.Event(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield Harness(
            adapters=adapters,
            database=database,
            objects=objects,
            client=client,
            worker=worker,
        )
    await close_resources(adapters)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- the walkthrough


async def test_a_submitted_job_becomes_a_downloadable_video(harness: Harness) -> None:
    """`docs/demo.md`, steps 1 to 5, in the order the script runs them."""
    # 1, 2. POST /v1/jobs
    submitted = await harness.submit()
    assert submitted.status_code == 202
    job = submitted.json()
    job_id = job["job_id"]
    assert job_id.startswith("job_")
    assert job["status"] == "QUEUED"
    assert job["stage"] == "INTAKE"
    assert job["artifact"] is None

    # The job and its work item are both there, written in one transaction.
    assert job_id in harness.database.jobs
    queued = [row for row in harness.database.work_items.values() if row.job_id == job_id]
    assert len(queued) == 1

    # 3. The worker claims it, runs it, and removes the queue row.
    assert await harness.turn() is True
    assert not harness.database.work_items

    read = await harness.get(f"/v1/jobs/{job_id}")
    assert read.status_code == 200
    done = read.json()
    assert done["status"] == "SUCCEEDED"
    assert done["stage"] == "DONE"
    assert done["progress"]["percent"] == 100
    assert done["failure"] is None
    assert done["artifact"] is not None
    assert done["artifact"]["role"] == ArtifactRole.PRIMARY.value
    assert done["artifact"]["media_type"] == "video/mp4"
    assert (
        done["artifact"]["content_url"]
        == f"/v1/artifacts/{done['artifact']['artifact_id']}/content"
    )

    listed_jobs = await harness.get("/v1/jobs")
    assert listed_jobs.status_code == 200
    assert [item["job_id"] for item in listed_jobs.json()["items"]] == [job_id]

    # 4. GET /v1/artifacts?job_id=... -- the learner's rows, and not the operator's log.
    listed = await harness.get(f"/v1/artifacts?job_id={job_id}")
    assert listed.status_code == 200
    items = listed.json()["items"]
    roles = {item["role"] for item in items}
    assert roles == {
        ArtifactRole.PRIMARY.value,
        ArtifactRole.POSTER.value,
        ArtifactRole.TRANSCRIPT.value,
    }
    assert ArtifactRole.LOG.value not in roles

    # The log row exists; the listing is what withholds it (D073, A6).
    written = [row for row in harness.database.artifacts.values() if row.job_id == job_id]
    assert any(row.role is ArtifactRole.LOG for row in written)
    assert {row.audience for row in written if row.role is ArtifactRole.LOG} == {Audience.OPERATOR}
    assert {item["artifact_id"] for item in items} == {
        row.artifact_id for row in written if row.audience is Audience.LEARNER
    }

    # 5. The bytes, and they are one of the committed lessons rather than a length that
    # happens to agree.
    artifact_id = done["artifact"]["artifact_id"]
    content = await harness.get(f"/v1/artifacts/{artifact_id}/content")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("video/mp4")

    brief = next(row for row in harness.database.briefs.values() if row.job_id == job_id)
    expected = video_for_brief(brief.brief_hash).path.read_bytes()
    assert content.content == expected
    assert _sha256(content.content) in {_sha256(video.path.read_bytes()) for video in LESSON_VIDEOS}
    assert content.headers["etag"] == f'"sha256:{_sha256(content.content)}"'


# --------------------------------------------------------------------------- the edges


async def test_a_second_user_reads_the_first_users_job(harness: Harness) -> None:
    """Both halves of D111 through the whole stack, because the walkthrough is where it shows.

    `QueryJob` passes the `AccessScope` and does not consult it: with nobody authenticated, the
    job id is the credential and a second caller holding one reads the job. The `404` is the part
    that survives, and it is asserted beside the `200` so the pair cannot be misread as the read
    answering `200` to everything. Adding a predicate to `QueryJob` later flips the first
    assertion, which is the point of pinning it.
    """
    submitted = await harness.submit()
    job_id = submitted.json()["job_id"]

    seen = await harness.get(f"/v1/jobs/{job_id}", user=OTHER_USER)
    assert seen.status_code == 200
    assert seen.json()["job_id"] == job_id

    # Existence is still checked. It is ownership that is not, and that is the decision.
    missing = await harness.get(f"/v1/jobs/{UNKNOWN_JOB_ID}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_a_fourth_concurrent_submit_is_refused(harness: Harness) -> None:
    """`admission_max_active_jobs` is 3, counted before anything is inserted (D090)."""
    for _ in range(settings.admission_max_active_jobs):
        accepted = await harness.submit()
        assert accepted.status_code == 202

    refused = await harness.submit()
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "TOO_MANY_ACTIVE_JOBS"

    # Not a rate limit: draining the queue makes room again.
    assert await harness.turn() is True
    assert (await harness.submit()).status_code == 202


async def test_a_failed_generation_is_a_two_hundred(harness: Harness) -> None:
    """`FAIL_ME` in the instruction, and the job that reports it is still a readable job."""
    submitted = await harness.submit(instruction=f"{INSTRUCTION} {FAIL_TOKEN}")
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]

    assert await harness.turn() is True

    read = await harness.get(f"/v1/jobs/{job_id}")
    assert read.status_code == 200
    failed = read.json()
    assert failed["status"] == "FAILED"
    assert failed["stage"] == "FAILED"
    assert failed["artifact"] is None
    assert failed["failure"] is not None
    assert failed["failure"]["code"] == "GENERATION_FAILED"

    # Nothing was published, so there is nothing to list.
    listed = await harness.get(f"/v1/artifacts?job_id={job_id}")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert len(harness.objects) == 0


async def test_health_reports_the_database(harness: Harness) -> None:
    """The endpoint compose's healthcheck hits, and the one the demo script asks first."""
    up = await harness.client.get("/health")
    assert up.status_code == 200
    assert up.json()["database"] == "up"

    harness.database.reachable = False
    down = await harness.client.get("/health")
    assert down.status_code == 503
    assert down.json()["database"] == "down"
