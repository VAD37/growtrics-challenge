"""API entrypoint. Builds the adapters, overrides the four providers, mounts the demo's routers.

The division of labour is the point. `app.composition` knows which class implements which port;
`app.api.install` knows which routers exist and how a failure is shaped; this file knows that the
two go together and that a connection pool is opened before a request is served and closed after
the last one. Nothing else in `src/app` imports an adapter.

Five endpoints under `/v1` and `/health`, which is exactly the surface `docs/demo.md` names.
`GET /v1/jobs/{job_id}/events` is built, tested, and deliberately not mounted: it is cut from the
demo (D091), and a cut endpoint that answers is not cut. `install(app, event_stream=True)` is the
one argument that restores it.

`/health` reports a real question. `SqlDatabaseProbe` opens a connection and runs `SELECT 1`, so
`{"database": "up"}` means the database answered rather than that a settings object was
constructed. Compose's healthcheck hits this endpoint, and the worker waits on it, so a constant
here would start a worker against a database nobody had reached.

Started with `python -m app.main` rather than `uvicorn app.main:app`, and the reason is one line
in `serve`: psycopg's async connections need a selector event loop, and choosing the loop is
something only the process that creates it can do.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import uvicorn
from fastapi import FastAPI

from app.api import install
from app.api.deps import (
    get_artifact_service,
    get_database_probe,
    get_job_service,
    get_principal_resolver,
    get_settings,
)
from app.composition import (
    Adapters,
    build_adapters,
    build_artifact_service,
    build_job_service,
    build_principal_resolver,
    close_resources,
    open_resources,
)
from app.config import settings

logger = logging.getLogger(__name__)

HOST: Final[str] = "0.0.0.0"
"""@audit every interface, which inside a container means the compose network and the published
port. Binding a loopback address would make the service unreachable from the host, and the
container is the isolation boundary here rather than the bind address."""

PORT: Final[int] = 8000


def build_app(adapters: Adapters) -> FastAPI:
    """The application, wired. Takes its adapters so a test can build one over doubles.

    Every provider in `app/api/deps.py` is replaced here and nowhere else. The API names ports and
    is handed implementations, which is what keeps the "api reaches no adapter" contract true
    while the endpoints still talk to Postgres.
    """
    app = FastAPI(
        title="AI chemistry video request service",
        version=adapters.settings.version,
        lifespan=_lifespan_for(adapters),
    )
    install(app)

    job_service = build_job_service(adapters)
    artifact_service = build_artifact_service(adapters)
    resolver = build_principal_resolver(adapters)

    app.dependency_overrides[get_settings] = lambda: adapters.settings
    app.dependency_overrides[get_job_service] = lambda: job_service
    app.dependency_overrides[get_artifact_service] = lambda: artifact_service
    app.dependency_overrides[get_database_probe] = lambda: adapters.probe
    app.dependency_overrides[get_principal_resolver] = lambda: resolver
    return app


def _lifespan_for(adapters: Adapters):
    """Open the pool and the bucket on startup, hand the connections back on shutdown.

    A lifespan rather than module-level work, because both are resources with an owner: uvicorn
    will not accept a request until this yields, and it runs the second half on SIGTERM, so
    `docker compose down` returns connections rather than leaving Postgres to time them out.

    A failure on the way up is not swallowed. The container exits and compose restarts it, which
    is the correct answer to a database that is not there yet and a much better one than a
    process that serves `500`s while looking healthy.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await open_resources(adapters)
        try:
            yield
        finally:
            await close_resources(adapters)

    return lifespan


app: Final[FastAPI] = build_app(build_adapters(settings))
"""The ASGI application. Importable as `app.main:app` for any server that wants it."""


async def _serve() -> None:
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="info", lifespan="on")
    )
    await server.serve()


def main() -> None:
    """Run the API on a selector event loop.

    psycopg's async connections need `add_reader`, which Windows's default `ProactorEventLoop`
    does not have: every connect raises `InterfaceError` naming the loop it was handed. On Linux,
    where this actually deploys, `SelectorEventLoop` is already the default and this argument
    changes nothing. `asyncio.set_event_loop_policy` would do the same and is deprecated for
    removal in 3.16, so it is not what this uses.

    It is also why the container runs `python -m app.main` instead of the `uvicorn` CLI: the CLI
    creates the loop itself, and `--loop asyncio` picks the platform default rather than this.
    """
    logging.basicConfig(level=logging.INFO)
    logger.info("api starting on %s:%s, version %s", HOST, PORT, settings.version)
    asyncio.run(_serve(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
