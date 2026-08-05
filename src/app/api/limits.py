"""How many bytes a request may spend before anything downstream reads them.

`config.request_max_body_bytes` was declared and nothing read it, so the accepted size was
whatever the server underneath happened to allow, which is not this number. This module is the
enforcement. It sits beside the routers rather than inside one because the cap is about the
transport, not about any one endpoint: once a handler holds the body, the memory is spent.

Two paths, because the client picks which one it uses:

* a `Content-Length` over the cap is refused before the first byte is read.
* no `Content-Length`, or one that lies, means counting the frames as they arrive and stopping
  the moment the running total crosses. The body is never accumulated in order to measure it.

Pure ASGI, like `SchemaVersionMiddleware` next door. `BaseHTTPMiddleware` buffers a streaming
response through a queue, and the artifact download this edge serves is a video.
"""

from contextlib import suppress
from typing import Final

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import envelope_for
from app.config import settings
from app.domain.errors import ErrorCode

BODY_TOO_LARGE_STATUS: Final[int] = 413

CAPPED_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})
"""The methods that carry a body by design. A `GET` has none, so it does not pay for counting."""

CAPPED_PATH_PREFIX: Final[str] = "/v1"
"""The contract's surface. `/health` is a liveness probe a container calls with no body."""


class _BodyTooLarge(Exception):
    """Raised inside the wrapped `receive` to stop the read at the cap.

    Never seen by a client. FastAPI wraps any exception out of body parsing into its own
    `400`, so the answer is written on the way out rather than left to that conversion.
    """


def is_capped(scope: Scope) -> bool:
    """Whether this request pays for the counting at all."""
    return (
        scope["type"] == "http"
        and scope["method"] in CAPPED_METHODS
        and scope["path"].startswith(CAPPED_PATH_PREFIX)
    )


def declared_length(scope: Scope) -> int | None:
    """The `Content-Length` the client claims, or `None` if it sent none or sent nonsense.

    Nonsense is not a rejection of its own. The counting path measures what actually arrives,
    so an unparseable header costs the client the same read as an absent one.
    """
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def rejection(limit: int) -> JSONResponse:
    """The frozen envelope at `413`, built the way `app/api/errors.py` builds every other one.

    @TODO `ErrorCode` has no `PAYLOAD_TOO_LARGE`, and adding a member needs a decision line
    (D062, D072). Until then the code is the closest existing one and the status carries the
    specific answer, which is the same compromise `http_exception_handler` already makes for a
    `404` on an unrouted path.
    """
    return JSONResponse(
        status_code=BODY_TOO_LARGE_STATUS,
        content=envelope_for(
            ErrorCode.INVALID_REQUEST,
            {"field": "body", "rule": "max_bytes", "limit": str(limit)},
        ).model_dump(mode="json", by_alias=True),
    )


class BodyLimitMiddleware:
    """Cap the request body on `/v1` write paths.

    Install it inside `SchemaVersionMiddleware` so the rejection still carries
    `X-Schema-Version`; see `app.api.install`.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app: ASGIApp = app
        self.max_bytes: int = settings.request_max_body_bytes if max_bytes is None else max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not is_capped(scope):
            await self.app(scope, receive, send)
            return

        declared = declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await rejection(self.max_bytes)(scope, receive, send)
            return

        counted = 0
        exceeded = False
        answered = False

        async def receive_counting() -> Message:
            nonlocal counted, exceeded
            message = await receive()
            if message["type"] != "http.request":
                return message
            counted += len(message.get("body", b""))
            if counted > self.max_bytes:
                exceeded = True
                raise _BodyTooLarge
            return message

        async def send_unless_exceeded(message: Message) -> None:
            nonlocal answered
            # Once the cap decided the request, whatever the application made of the aborted
            # read is discarded. FastAPI turns the raised exception into a `400` about parsing,
            # and that would be the wrong answer to a body that was simply too big.
            #
            # An application that had already started a response keeps it: half a response
            # followed by a second one is worse than an oversized body nobody stopped.
            if exceeded and not answered:
                return
            answered = True
            await send(message)

        with suppress(_BodyTooLarge):
            await self.app(scope, receive_counting, send_unless_exceeded)

        if exceeded and not answered:
            await rejection(self.max_bytes)(scope, receive, send)
