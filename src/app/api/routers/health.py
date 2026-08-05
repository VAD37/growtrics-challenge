"""`GET /health`: is the database reachable (`docs/demo.md`, "Surface").

Liveness alone answers "the process is up", which is the question nobody asks during a demo.
The probe is a port, so this router needs no database to be tested and the storage lane owns
what "reachable" means.

Outside `/v1` on purpose: it is an operational endpoint, not part of the contract a client
codes against, and it resolves no principal because there is nothing here to scope.
"""

from typing import Final

from fastapi import APIRouter, Response

from app.api.deps import DatabaseProbeDep, SettingsDep
from app.api.schemas.common import HealthView

router: Final[APIRouter] = APIRouter(tags=["health"])

DEGRADED_STATUS: Final[int] = 503


@router.get("/health", response_model=HealthView)
async def health(response: Response, probe: DatabaseProbeDep, config: SettingsDep) -> HealthView:
    """`200` when the database answers, `503` when it does not.

    A probe that raises is a down database rather than a `500`: a connection error is the
    answer to the question this endpoint asks, not an accident that happened while asking it.
    """
    try:
        reachable = await probe.ping()
    except Exception:
        reachable = False
    if not reachable:
        response.status_code = DEGRADED_STATUS
    return HealthView(
        status="ok" if reachable else "degraded",
        database="up" if reachable else "down",
        version=config.version,
    )
