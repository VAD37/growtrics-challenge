"""The request body cap, on both paths a client can take to get past it.

`config.request_max_body_bytes` was declared and nothing read it, so the accepted size was
whatever the server underneath happened to allow. Every assertion below reads the cap from the
setting rather than from a literal, so the number and its enforcement cannot drift apart again.

Half of this file drives the ASGI application directly instead of through `TestClient`. That is
not scenery. `TestClient` joins a generator body into one `bytes` before the application sees
it, which is the buffering the chunked path exists to avoid, and a response it collects into a
`BytesIO` cannot show whether the bytes were streamed or held.
"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Any, Final

from api_fakes import FIXED_CONTENT_HASH, FakeArtifactService, make_artifact
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.api.errors import ENVELOPE_KEYS, ERROR_BODY_KEYS
from app.api.limits import BODY_TOO_LARGE_STATUS, BodyLimitMiddleware, declared_length
from app.api.schemas.common import SCHEMA_VERSION, SCHEMA_VERSION_HEADER
from app.config import settings
from app.domain.records import ContentStream

CAP: Final[int] = settings.request_max_body_bytes
JSON_HEADERS: Final[dict[str, str]] = {"Content-Type": "application/json"}
JSON_CONTENT_TYPE: Final[tuple[bytes, bytes]] = (b"content-type", b"application/json")

FRAME: Final[int] = 4096
"""Frame size for the chunked tests. Divides the cap, so the crossing frame is unambiguous."""


def body_of(size: int) -> bytes:
    """A valid `POST /v1/jobs` document padded to exactly `size` bytes.

    The padding is whitespace inside the object, so the bytes on the wire grow while the
    document a router parses does not. A body that is merely large is otherwise unreachable:
    the instruction caps at 500 characters, so nothing schema-valid gets near 64 KiB by itself.
    """
    document = b'{"instruction": "why do atoms form covalent bonds"}'
    padding = size - len(document)
    assert padding >= 0, "the demo document is already larger than the requested size"
    return document[:-1] + b" " * padding + b"}"


def http_scope(
    method: str, path: str, headers: Iterable[tuple[bytes, bytes]] = ()
) -> dict[str, Any]:
    """The scope uvicorn would build. Header names are lowercase bytes, as ASGI requires."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }


class Wire:
    """One request driven straight at the application, frame by frame.

    `pulled` is how many body frames the application actually asked for. A limiter that reads a
    whole body before measuring it shows up here as a `pulled` equal to the frame count.
    """

    def __init__(self, frames: list[bytes]) -> None:
        self.frames: list[bytes] = frames
        self.pulled: int = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        if self.pulled == len(self.frames):
            # What a real server does once the body is spent: nothing, until the client hangs
            # up. Returning `http.disconnect` here would cancel a streaming response instead.
            await asyncio.Event().wait()
        frame = self.frames[self.pulled]
        self.pulled += 1
        return {"type": "http.request", "body": frame, "more_body": self.pulled < len(self.frames)}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        starts = [message for message in self.sent if message["type"] == "http.response.start"]
        assert len(starts) == 1, f"expected one response start, got {len(starts)}"
        return int(starts[0]["status"])

    @property
    def body(self) -> bytes:
        return b"".join(
            message.get("body", b"")
            for message in self.sent
            if message["type"] == "http.response.body"
        )


async def drive(app: FastAPI, scope: dict[str, Any], frames: list[bytes]) -> Wire:
    wire = Wire(frames)
    await app(scope, wire.receive, wire.send)
    return wire


async def nothing(scope: Scope, receive: Receive, send: Send) -> None:
    """An application the middleware can wrap when a test only wants to read its cap."""


# --------------------------------------------------------------------------- the declared size


def test_a_body_one_byte_under_the_cap_is_accepted(client: TestClient) -> None:
    response = client.post("/v1/jobs", content=body_of(CAP - 1), headers=JSON_HEADERS)
    assert response.status_code == 202


def test_a_body_one_byte_over_the_cap_is_rejected(client: TestClient) -> None:
    response = client.post("/v1/jobs", content=body_of(CAP + 1), headers=JSON_HEADERS)
    assert response.status_code == BODY_TOO_LARGE_STATUS


def test_the_rejection_is_the_frozen_envelope(client: TestClient) -> None:
    body = client.post("/v1/jobs", content=body_of(CAP + 1), headers=JSON_HEADERS).json()
    assert set(body) == set(ENVELOPE_KEYS)
    assert set(body["error"]) == set(ERROR_BODY_KEYS)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"] == {"field": "body", "rule": "max_bytes", "limit": str(CAP)}


def test_the_rejection_still_carries_the_schema_version(client: TestClient) -> None:
    # The cap answers before any router runs, which is where a header set downstream is missed.
    response = client.post("/v1/jobs", content=body_of(CAP + 1), headers=JSON_HEADERS)
    assert response.headers[SCHEMA_VERSION_HEADER] == SCHEMA_VERSION
    assert response.json()["schema_version"] == SCHEMA_VERSION


async def test_an_oversized_content_length_is_refused_before_the_body_is_read(
    api: FastAPI,
) -> None:
    # The client said how much it was about to send and that was enough. No frame is pulled,
    # and nothing downstream of the cap sees the request at all.
    scope = http_scope(
        "POST",
        "/v1/jobs",
        [JSON_CONTENT_TYPE, (b"content-length", str(CAP + 1).encode())],
    )
    wire = await drive(api, scope, [b"x" * (CAP + 1)])
    assert wire.status == BODY_TOO_LARGE_STATUS
    assert wire.pulled == 0


# --------------------------------------------------------------------------- the arriving size


def test_a_lying_content_length_is_still_rejected(client: TestClient) -> None:
    # The header is a claim, not a measurement. httpx leaves an explicit one alone, so this
    # request declares 42 bytes and then sends 64 KiB of them.
    response = client.post(
        "/v1/jobs",
        content=body_of(CAP + 1),
        headers={**JSON_HEADERS, "Content-Length": "42"},
    )
    assert response.status_code == BODY_TOO_LARGE_STATUS
    assert response.json()["error"]["details"]["limit"] == str(CAP)


async def test_a_chunked_body_over_the_cap_is_rejected_mid_read(api: FastAPI) -> None:
    # No `Content-Length`, twice the cap on the wire. The read has to stop on its own.
    frames = [b"x" * FRAME] * (2 * CAP // FRAME)
    wire = await drive(api, http_scope("POST", "/v1/jobs", [JSON_CONTENT_TYPE]), frames)

    assert wire.status == BODY_TOO_LARGE_STATUS
    # It stopped on the frame that crossed the cap. Nothing ever held more than the cap plus
    # one frame, and the second half of the body was never asked for.
    assert wire.pulled == CAP // FRAME + 1
    assert wire.pulled * FRAME <= CAP + FRAME
    assert wire.pulled < len(frames)


async def test_a_chunked_body_under_the_cap_is_read_whole(api: FastAPI) -> None:
    document = body_of(CAP - 1)
    frames = [document[start : start + FRAME] for start in range(0, len(document), FRAME)]
    wire = await drive(api, http_scope("POST", "/v1/jobs", [JSON_CONTENT_TYPE]), frames)

    assert wire.status == 202
    assert wire.pulled == len(frames)


# --------------------------------------------------------------------------- reach of the cap


async def test_a_get_is_not_capped(api: FastAPI) -> None:
    # A GET has no body and must not pay for the counting. The frame below is never pulled.
    wire = await drive(api, http_scope("GET", "/v1/artifacts"), [b"x" * (CAP + 1)])
    assert wire.status == 200
    assert wire.pulled == 0


def test_the_cap_is_the_configured_setting() -> None:
    # The defect this file closes: the setting existed and no code read it.
    assert BodyLimitMiddleware(nothing).max_bytes == settings.request_max_body_bytes


def test_an_unreadable_content_length_falls_through_to_counting() -> None:
    assert declared_length(http_scope("POST", "/v1/jobs", [(b"content-length", b"lots")])) is None
    assert declared_length(http_scope("POST", "/v1/jobs")) is None
    assert declared_length(http_scope("POST", "/v1/jobs", [(b"content-length", b"7")])) == 7


# --------------------------------------------------------------------------- no regression


async def test_the_artifact_download_is_still_streamed_not_buffered(
    api: FastAPI, artifact_service: FakeArtifactService
) -> None:
    """Why both middlewares are pure ASGI (`app/api/__init__.py`).

    Buffering shows up as an order: every chunk read, then the first byte sent. Streaming
    interleaves the two, and this asserts the interleaving rather than the final bytes, which
    are identical either way.
    """
    trace: list[str] = []

    async def chunks() -> AsyncIterator[bytes]:
        for index in range(4):
            trace.append(f"read {index}")
            yield b"x" * 16

    artifact = artifact_service.load(make_artifact())
    artifact_service.stream = ContentStream(
        media_type="video/mp4",
        size_bytes=64,
        content_hash=FIXED_CONTENT_HASH,
        filename="lesson.mp4",
        disposition="inline",
        chunks=chunks(),
    )

    wire = Wire([])

    async def watched_send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            trace.append("sent")
        await wire.send(message)

    scope = http_scope("GET", f"/v1/artifacts/{artifact.artifact_id}/content")
    await api(scope, wire.receive, watched_send)

    assert wire.status == 200
    assert wire.body == b"x" * 64
    assert trace.index("sent") < trace.index("read 3"), trace
