"""API entrypoint and composition root.

@TODO build adapters, wire ports, and mount the `/v1` routers here (docs/plan/03-module-layout.md).
Until then this module exists to prove the image boots and the service is reachable.
"""

from fastapi import FastAPI

from app.config import settings

app = FastAPI(title="AI chemistry video request service", version=settings.version)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness only.

    @TODO stage 1 requires this to report database reachability (docs/plan/13-mvp.md).
    """
    return {"status": "ok", "version": settings.version}
