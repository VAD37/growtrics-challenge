"""`GET /v1/jobs/{job_id}/events`: what a client is fed, and when.

Half of this file drives the ASGI application directly rather than through `TestClient`, for
the same reason `body_limit_test.py` does. A `TestClient` collects a streaming body into one
buffer, and a buffer cannot answer the only question that separates this endpoint from a poll:
did the byte reach the client before the job moved on. The interleaving test below makes the
job advance *because* a frame arrived, so a buffered implementation cannot produce more than
one event and fails on the assertion rather than on a timeout.

Timings come from a `Settings` built here with a hundredth-of-a-second tick, injected through
`get_settings`. Nothing sleeps for a wall-clock second and nothing reads the shipped defaults.
"""

import asyncio
import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

import pytest
from api_fakes import FakeJobService, make_job
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from app.api.deps import get_settings
from app.api.errors import ENVELOPE_KEYS
from app.api.routers.events import STREAM_HEADERS, resume_version
from app.api.schemas.common import SCHEMA_VERSION, SCHEMA_VERSION_HEADER
from app.api.schemas.events import (
    HEARTBEAT,
    JOB_EVENT_KEYS,
    RECONNECT_AFTER,
    SSE_MEDIA_TYPE,
    STREAM_END_KEYS,
    EventName,
    StreamEndReason,
)
from app.config import Settings
from app.domain.access import AccessScope
from app.domain.enums import JobStatus, StageName
from app.domain.ids import derive_job_id, mint_request_key
from app.domain.records import JobRecord

type EventJson = Mapping[str, object]
type FrameWatcher = Callable[[str], None]

MISSING_JOB_ID: Final[str] = derive_job_id(mint_request_key(bytes(range(16, 32))))
"""A well-formed id no fake ever loads. A malformed one would be `400` and prove nothing."""

TICK: Final[float] = 0.01

STREAM_CAP: Final[float] = 0.5
"""The stream's own lifetime in most tests. Well past what a working implementation needs, and
short enough that a broken one ends and fails an assertion instead of hanging."""

TIMEOUT: Final[float] = 3.0
"""How long a test waits before calling it a hang. Six times the cap it is watching."""


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def job_service() -> CountingJobService:
    """Overrides the conftest fake for this module only, to count reads behind the seam."""
    return CountingJobService()


@pytest.fixture
def stream_settings() -> Settings:
    return Settings(
        job_stream_tick_seconds=TICK,
        job_stream_heartbeat_seconds=STREAM_CAP,
        job_stream_max_seconds=STREAM_CAP,
    )


@pytest.fixture
def stream_api(api: FastAPI, stream_settings: Settings) -> FastAPI:
    """The conftest app with the stream's three timings replaced by test-speed ones."""
    api.dependency_overrides[get_settings] = lambda: stream_settings
    return api


@pytest.fixture
def stream_client(stream_api: FastAPI) -> Iterator[TestClient]:
    with TestClient(stream_api) as test_client:
        yield test_client


class CountingJobService(FakeJobService):
    """`FakeJobService` that records how many times the edge read the job.

    The count is the endpoint's cost made visible: one read before the stream opens, then one
    per tick. It is also how a disconnect is proved to have stopped the loop. `ticked` fires on
    every read, so a test waits for the loop to turn instead of sleeping and hoping.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gets: int = 0
        self.ticked: asyncio.Event = asyncio.Event()

    async def get(self, scope: AccessScope, job_id: str) -> JobRecord:
        self.gets += 1
        self.ticked.set()
        return await super().get(scope, job_id)

    async def wait_for_ticks(self, count: int) -> None:
        for _ in range(count):
            self.ticked.clear()
            await asyncio.wait_for(self.ticked.wait(), timeout=TIMEOUT)


class Stream:
    """One SSE connection driven straight at the application, frame by frame.

    Frames are captured as they are sent rather than joined at the end, and `on_frame` runs
    inside `send`, so a test can change the world at the exact moment a byte reaches the
    client. `receive` blocks until `disconnect` is set, which is what a real connection does
    while the server has nothing to read from it.
    """

    def __init__(self) -> None:
        self.frames: list[str] = []
        self.start: Message | None = None
        self.disconnect: asyncio.Event = asyncio.Event()
        self.on_frame: FrameWatcher = lambda frame: None

    async def receive(self) -> Message:
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.start = message
        elif message["type"] == "http.response.body" and message.get("body"):
            frame = bytes(message["body"]).decode()
            self.frames.append(frame)
            self.on_frame(frame)

    @property
    def status(self) -> int:
        assert self.start is not None, "the response never started"
        return int(self.start["status"])


def http_scope(path: str, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The scope uvicorn would build for a `GET`.

    `spec_version` is 2.3 deliberately. Starlette only runs its disconnect watcher below 2.4;
    at 2.4 and above it expects the server to raise from `send` instead, which a test double
    cannot do faithfully.
    """
    raw = [(b"host", b"testserver")]
    for name, value in (headers or {}).items():
        raw.append((name.lower().encode(), value.encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


def events_path(job_id: str) -> str:
    return f"/v1/jobs/{job_id}/events"


# --------------------------------------------------------------------------- frame parsing


def blocks(body: str) -> list[str]:
    """Split an SSE body into its blocks. The blank line is the delimiter, per the spec."""
    return [block for block in body.split("\n\n") if block]


def named(body: str, name: EventName) -> list[EventJson]:
    """The `data:` documents of every block carrying this `event:` name."""
    found: list[EventJson] = []
    for block in blocks(body):
        if f"event: {name}" not in block:
            continue
        payload = block.split("data: ", 1)[1]
        document = json.loads(payload)
        assert isinstance(document, dict), payload
        found.append(document)
    return found


def event_ids(body: str) -> list[str]:
    return [
        line.removeprefix("id: ")
        for block in blocks(body)
        for line in block.splitlines()
        if line.startswith("id: ")
    ]


def percent_of(event: EventJson) -> int:
    """The number on the bar, out of the `ProgressView` this event reuses."""
    progress = event["progress"]
    assert isinstance(progress, dict), progress
    percent = progress["percent"]
    assert isinstance(percent, int), percent
    return percent


def heartbeats(body: str) -> int:
    return body.count(HEARTBEAT.strip())


# --------------------------------------------------------------------------- the response


def test_the_response_is_an_event_stream_with_no_content_length(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=4))
    response = stream_client.get(events_path(job.job_id))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(SSE_MEDIA_TYPE)
    # A length would mean the whole stream was known before it started.
    assert "content-length" not in response.headers
    for header, value in STREAM_HEADERS.items():
        assert response.headers[header] == value
    assert response.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION


def test_a_progress_event_carries_what_a_progress_bar_needs(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=4))
    body = stream_client.get(events_path(job.job_id)).text

    (event,) = named(body, EventName.PROGRESS)
    assert set(event) == set(JOB_EVENT_KEYS)
    assert event["status"] == JobStatus.SUCCEEDED
    assert event["stage"] == StageName.DONE
    assert event["version"] == 4
    assert event["progress"] == {"percent": 100, "step": "done", "message": "finished"}
    assert event["schema_version"] == SCHEMA_VERSION
    # The SSE id is the version, which is what `Last-Event-ID` replays on a reconnect.
    assert event_ids(body) == ["4"]


# --------------------------------------------------------------------------- transitions


async def test_events_reach_the_client_before_the_job_reaches_the_next_version(
    stream_api: FastAPI, job_service: CountingJobService
) -> None:
    """The non-buffering proof, and the reason both middlewares are pure ASGI.

    The job only advances when a frame is delivered, so the sequence below is a statement about
    ordering rather than about content. An implementation that buffered the response would
    deliver nothing until the generator finished, the generator would never see a version move,
    and this would collect one event at version 0 instead of four in order.
    """
    steps = [
        make_job(status=JobStatus.RUNNING, stage=StageName.PREPARING, version=1),
        make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING, version=2),
        make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=3),
    ]
    job = job_service.load(make_job(status=JobStatus.QUEUED, stage=StageName.INTAKE, version=0))
    pending = iter(steps)
    stream = Stream()

    def advance(frame: str) -> None:
        if f"event: {EventName.PROGRESS}" not in frame:
            return
        following = next(pending, None)
        if following is not None:
            job_service.load(following)

    stream.on_frame = advance
    await asyncio.wait_for(
        stream_api(http_scope(events_path(job.job_id)), stream.receive, stream.send),
        timeout=TIMEOUT,
    )

    assert stream.status == 200
    body = "".join(stream.frames)
    progress = named(body, EventName.PROGRESS)
    assert [event["version"] for event in progress] == [0, 1, 2, 3]
    assert [event["stage"] for event in progress] == ["INTAKE", "PREPARING", "GENERATING", "DONE"]
    # D042: the bar never goes backwards, and each of these arrived on its own frame.
    assert [percent_of(event) for event in progress] == [10, 25, 60, 100]
    assert event_ids(body) == ["0", "1", "2", "3"]


async def test_each_transition_is_its_own_frame(
    stream_api: FastAPI, job_service: CountingJobService
) -> None:
    """Four states, four separate writes. A client parses blocks, never a batch."""
    steps = [
        make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING, version=1),
        make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=2),
    ]
    job = job_service.load(make_job(status=JobStatus.QUEUED, stage=StageName.INTAKE, version=0))
    pending = iter(steps)
    stream = Stream()

    def advance(frame: str) -> None:
        following = next(pending, None)
        if f"event: {EventName.PROGRESS}" in frame and following is not None:
            job_service.load(following)

    stream.on_frame = advance
    await asyncio.wait_for(
        stream_api(http_scope(events_path(job.job_id)), stream.receive, stream.send),
        timeout=TIMEOUT,
    )

    written = [frame for frame in stream.frames if "event: " in frame]
    assert len(written) == 4, written
    assert all(frame.endswith("\n\n") for frame in written)
    assert all(frame.count("data: ") == 1 for frame in written)


# --------------------------------------------------------------------------- silence


async def test_an_unchanged_job_sends_heartbeats_and_no_second_event(
    api: FastAPI, job_service: CountingJobService
) -> None:
    api.dependency_overrides[get_settings] = lambda: Settings(
        job_stream_tick_seconds=TICK,
        job_stream_heartbeat_seconds=0.03,
        job_stream_max_seconds=0.3,
    )
    job = job_service.load(make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING))
    stream = Stream()

    await asyncio.wait_for(
        api(http_scope(events_path(job.job_id)), stream.receive, stream.send), timeout=TIMEOUT
    )

    body = "".join(stream.frames)
    # The tick is a metronome; the stream is not. One event for the state the client did not
    # have, then nothing but proof the connection is alive.
    assert len(named(body, EventName.PROGRESS)) == 1
    assert heartbeats(body) >= 2
    assert job_service.gets > heartbeats(body), "a heartbeat per tick would be a metronome"


# --------------------------------------------------------------------------- closing


def test_a_terminal_job_closes_the_stream_instead_of_hanging(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=9))
    body = stream_client.get(events_path(job.job_id)).text

    (end,) = named(body, EventName.END)
    assert set(end) == set(STREAM_END_KEYS)
    assert end["reason"] == StreamEndReason.TERMINAL
    assert end["reconnect"] is False
    assert end["version"] == 9
    # It never ticked. A finished job costs the read that opened the connection and nothing.
    assert job_service.gets == 1


def test_a_failed_job_closes_the_stream_too(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    """FAILED is terminal. The stream ends because the job stopped, not because it succeeded."""
    job = job_service.load(make_job(status=JobStatus.FAILED, stage=StageName.FAILED, version=2))
    body = stream_client.get(events_path(job.job_id)).text

    (event,) = named(body, EventName.PROGRESS)
    assert event["status"] == JobStatus.FAILED
    (end,) = named(body, EventName.END)
    assert end["reason"] == StreamEndReason.TERMINAL


def test_the_hard_lifetime_cap_closes_the_stream(
    api: FastAPI, job_service: CountingJobService
) -> None:
    api.dependency_overrides[get_settings] = lambda: Settings(
        job_stream_tick_seconds=TICK,
        job_stream_heartbeat_seconds=STREAM_CAP,
        job_stream_max_seconds=0.1,
    )
    job = job_service.load(make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING))

    with TestClient(api) as client:
        body = client.get(events_path(job.job_id)).text

    (end,) = named(body, EventName.END)
    assert end["reason"] == StreamEndReason.MAX_DURATION
    # The one end reason that says come back: the job is still running and has more to say.
    assert end["reconnect"] is True
    assert RECONNECT_AFTER[StreamEndReason.MAX_DURATION] is True


def test_the_end_frame_carries_no_event_id(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    """A reconnect resumes from the last state seen, never from a control frame."""
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=6))
    body = stream_client.get(events_path(job.job_id)).text

    assert event_ids(body) == ["6"]
    end_block = next(block for block in blocks(body) if f"event: {EventName.END}" in block)
    assert "id: " not in end_block


def test_every_end_reason_says_whether_to_reconnect() -> None:
    assert set(RECONNECT_AFTER) == set(StreamEndReason)


# --------------------------------------------------------------------------- resume


def test_last_event_id_suppresses_a_version_already_seen(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=7))
    body = stream_client.get(events_path(job.job_id), headers={"Last-Event-ID": "7"}).text

    assert named(body, EventName.PROGRESS) == []
    (end,) = named(body, EventName.END)
    assert end["reason"] == StreamEndReason.TERMINAL


def test_last_event_id_behind_the_job_still_replays_the_current_state(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=7))
    body = stream_client.get(events_path(job.job_id), headers={"Last-Event-ID": "6"}).text

    (event,) = named(body, EventName.PROGRESS)
    assert event["version"] == 7


def test_a_malformed_last_event_id_is_ignored_rather_than_refused(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    job = job_service.load(make_job(status=JobStatus.SUCCEEDED, stage=StageName.DONE, version=7))
    response = stream_client.get(events_path(job.job_id), headers={"Last-Event-ID": "banana"})

    assert response.status_code == 200
    assert len(named(response.text, EventName.PROGRESS)) == 1


def test_resume_version_reads_what_a_browser_sends() -> None:
    assert resume_version("12") == 12
    assert resume_version(" 12 ") == 12
    assert resume_version(None) is None
    assert resume_version("") is None
    assert resume_version("banana") is None


# --------------------------------------------------------------------------- refusals


def test_an_unknown_job_is_a_404_envelope_and_not_an_open_stream(
    stream_client: TestClient,
) -> None:
    response = stream_client.get(events_path(MISSING_JOB_ID))

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert set(response.json()) == set(ENVELOPE_KEYS)
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_a_malformed_job_id_never_reaches_the_service(
    stream_client: TestClient, job_service: CountingJobService
) -> None:
    response = stream_client.get(events_path("not-a-job-id"))

    assert response.status_code == 400
    assert job_service.gets == 0


# --------------------------------------------------------------------------- disconnect


async def test_a_client_disconnect_stops_the_polling_loop(
    api: FastAPI, job_service: CountingJobService
) -> None:
    """The loop belongs to the connection. When the connection goes, the reads go with it.

    The cap is set an order of magnitude past the test's patience on purpose: with it in reach
    the connection would close on its own and this would prove nothing about the disconnect.
    The job never reaches a terminal status either, so hanging up is the only way out.
    """
    api.dependency_overrides[get_settings] = lambda: Settings(
        job_stream_tick_seconds=TICK,
        job_stream_heartbeat_seconds=TIMEOUT * 10,
        job_stream_max_seconds=TIMEOUT * 10,
    )
    job = job_service.load(make_job(status=JobStatus.RUNNING, stage=StageName.GENERATING))
    stream = Stream()
    connection = asyncio.create_task(
        api(http_scope(events_path(job.job_id)), stream.receive, stream.send)
    )

    await job_service.wait_for_ticks(3)

    stream.disconnect.set()
    await asyncio.wait_for(connection, timeout=TIMEOUT)

    settled = job_service.gets
    await asyncio.sleep(TICK * 20)
    assert job_service.gets == settled, "the stream kept reading after the client left"
