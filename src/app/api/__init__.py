"""API context. FastAPI lives here and nowhere else. No domain rules, no adapters.

`install(app)` is the whole surface this package offers the composition root: routers,
exception handlers, and the response header that carries the schema version. One call, so
`main.py` cannot mount half an API, and no router is reachable without the handlers that shape
its failures.

The header is set by ASGI middleware rather than by each handler because it has to be on
responses no handler produces: a `304` with no body, a `404` for an unrouted path, an error
envelope from a validation failure. A header written per endpoint is a header missing from the
responses nobody remembered to write.
"""

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import install_error_handlers
from app.api.limits import BodyLimitMiddleware
from app.api.routers import artifacts, events, health, jobs
from app.api.schemas.common import SCHEMA_VERSION, SCHEMA_VERSION_HEADER

__all__ = ["SchemaVersionMiddleware", "install"]


class SchemaVersionMiddleware:
    """Stamp `X-Schema-Version` on every HTTP response.

    Pure ASGI rather than `BaseHTTPMiddleware`: the latter buffers a streaming response
    through a queue, and this edge streams videos.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[SCHEMA_VERSION_HEADER] = SCHEMA_VERSION
            await send(message)

        await self.app(scope, receive, send_with_version)


def install(app: FastAPI) -> None:
    """Mount the demo's endpoints on an app the composition root owns.

    Deliberately not a `create_app()`: the composition root builds the application, chooses the
    lifespan, and overrides the providers in `deps.py`. This function adds the edge to it.

    The middleware order is deliberate. Starlette runs the last one added outermost, so the body
    cap is added first and the version header wraps it: a `413` written before any router runs
    still leaves with `X-Schema-Version` on it.
    """
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(SchemaVersionMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(events.router)
    app.include_router(artifacts.router)
