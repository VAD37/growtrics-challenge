"""`GET /v1/jobs/{job_id}/events`. The job's stage transitions, pushed as they happen.

`POST /v1/jobs` still answers `202` and still queues. It has to: a streaming submit cannot be
reconnected without re-submitting, and a retry would mint a second job (there is no replay
protection, see `app/domain/ids.py`). So the stream is a second endpoint the client opens
against a job it already has an id for.

Server-Sent Events rather than websockets. Nothing travels upstream, SSE survives the proxies
that mangle an upgrade, and a browser reconnects an `EventSource` on its own and replays
`Last-Event-ID` while doing it -- which is the resume protocol, for free, in the client we do
not write.

WHAT THIS COSTS, PLAINLY. It is polling wearing a streaming face. Every open connection runs
one `JobService.get` every `job_stream_tick_seconds`, so at the 1.0s default a thousand
concurrent streams is a thousand primary-key reads a second against `jobs`, forever, whether or
not a single one of those jobs moved. On top of that each stream holds a connection and a task
for as long as its job runs. The database load is the same load the client's own polling would
have made; what changed is that the server pays it, at a rate the client no longer chooses, and
that transitions now reach the client within a tick instead of within a poll interval. The tick
is the whole defect and it is removable: `LISTEN`/`NOTIFY` on a `jobs` update lets a connection
sleep until the row actually changes, which turns a thousand idle streams into a thousand idle
sockets and no queries at all. That needs a dedicated database connection per API process and a
notification channel keyed by job, which is a storage-lane change, so it is a `@TODO` and not
this module.

Nothing here reads a table, and nothing here reads the clock for anything but its own timers.
The only source of job state is the same `JobService.get` the polling endpoint uses.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from app.api.deps import JobServiceDep, ScopeDep, SettingsDep
from app.api.errors import ERROR_RESPONSES
from app.api.ports import JobService
from app.api.routers.jobs import JobIdPath
from app.api.schemas.events import (
    HEARTBEAT,
    SSE_MEDIA_TYPE,
    EventFrame,
    StreamEndReason,
    end_frame,
    progress_frame,
)
from app.config import Settings
from app.domain.access import AccessScope
from app.domain.enums import TERMINAL_JOB_STATUSES
from app.domain.errors import DomainError
from app.domain.ids import JobId
from app.domain.records import JobRecord

router: Final[APIRouter] = APIRouter(prefix="/v1", tags=["jobs"], responses=ERROR_RESPONSES)

LAST_EVENT_ID_HEADER: Final[str] = "Last-Event-ID"
"""The resume header, spelled the way the SSE specification spells it. A browser sets it
itself on every reconnect; a hand-written client sets it from the last `version` it saw."""

LastEventIdHeader = Annotated[str | None, Header(alias=LAST_EVENT_ID_HEADER)]

STREAM_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "X-Content-Type-Options": "nosniff",
}
"""Three instructions to whatever sits between this process and the browser.

`no-transform` stops a proxy compressing the body, which is what makes a proxy hold it. nginx
buffers a proxied response by default and `X-Accel-Buffering: no` is the documented way to
turn that off for one response, and both of them together are why the middlewares in
`app/api/__init__.py` are pure ASGI: a buffer anywhere on the path turns this endpoint back
into a slow poll. `Content-Length` is absent on purpose and Starlette omits it for a streaming
body -- a length would mean the whole stream was known before it started.
"""


def resume_version(header: str | None) -> int | None:
    """The version a reconnecting client says it already has, or `None`.

    Unparseable input is `None` rather than a `400`. The header is set by the browser from a
    frame we wrote, so a malformed one means something in the middle rewrote it, and refusing
    the connection over that would strand a client that only wanted the state it is missing.

    @audit the value is client-supplied and unverified. It is used for one equality test
    against the row's own `version` and never as an index, a bound, or a query parameter, so
    the worst a lie achieves is suppressing one event the liar asked for.
    """
    if header is None:
        return None
    try:
        return int(header.strip())
    except ValueError:
        return None


async def job_frames(
    *,
    jobs: JobService,
    scope: AccessScope,
    job_id: JobId,
    first: JobRecord,
    seen: int | None,
    settings: Settings,
) -> AsyncIterator[EventFrame]:
    """Tick, compare, and write only when the job actually moved.

    `first` is the read the route already did to decide the `404`, so a connection costs one
    query before its first tick rather than two. `seen` starts at `Last-Event-ID`, which is
    what makes a reconnect silent until something new happens.

    The comparison is `!=` and not `>`. A client that reports a version the job never reached
    gets one duplicate event and then behaves; `>` would let the same lie suppress every event
    for the life of the connection.

    Every write resets the heartbeat timer, so a busy job pays for no pings and an idle one
    gets them at exactly the configured interval.
    """
    started = time.monotonic()
    beat = started
    job = first
    while True:
        if job.version != seen:
            seen = job.version
            beat = time.monotonic()
            yield progress_frame(job)

        if job.status in TERMINAL_JOB_STATUSES:
            # A finished job holds no connection. Its last state went out above; this says
            # so, and says not to come back, because reconnecting would repeat this exchange.
            yield end_frame(job, StreamEndReason.TERMINAL)
            return

        if time.monotonic() - started >= settings.job_stream_max_seconds:
            # An unbounded stream is a resource leak with a feature's name on it. The client
            # is told to reconnect, so the cap costs one round trip and not a lost update.
            yield end_frame(job, StreamEndReason.MAX_DURATION)
            return

        # @TODO this sleep is the whole cost of the endpoint: one `JobService.get` per open
        # connection per tick, load the database pays whether or not the job moved. Postgres
        # `LISTEN`/`NOTIFY` on a `jobs` update replaces it with a wait that wakes only on a
        # real change; it needs a dedicated connection per process and a per-job channel, both
        # of which live in the storage lane. Until then the tick is the resolution of the
        # lifetime cap as well, so the stream can overshoot `job_stream_max_seconds` by one.
        await asyncio.sleep(settings.job_stream_tick_seconds)

        if time.monotonic() - beat >= settings.job_stream_heartbeat_seconds:
            beat = time.monotonic()
            yield HEARTBEAT

        try:
            job = await jobs.get(scope, job_id)
        except DomainError:
            # The response started long ago, so no exception handler can shape this into an
            # envelope any more. Saying why in the last frame beats a body that simply stops.
            yield end_frame(job, StreamEndReason.GONE)
            return


@router.get("/jobs/{job_id}/events", response_class=StreamingResponse)
async def stream_job_events(
    job_id: JobIdPath,
    scope: ScopeDep,
    jobs: JobServiceDep,
    settings: SettingsDep,
    last_event_id: LastEventIdHeader = None,
) -> StreamingResponse:
    """One connection, one job, stage transitions until it finishes or the cap expires.

    The existence check happens here and not inside the generator, on purpose: once the
    generator has yielded its first frame the status line is already sent, and a `404`
    discovered afterwards would arrive as an error inside a `200`. A client never has to
    unpick that, because `jobs.get` raising `JOB_NOT_FOUND` here leaves through the same
    envelope every other route produces, before the stream exists.

    `JobIdPath` is imported from the polling router rather than restated, so a malformed id is
    the same `400` on both endpoints and neither becomes an id-shape oracle the other is not.
    """
    job = await jobs.get(scope, job_id)
    return StreamingResponse(
        job_frames(
            jobs=jobs,
            scope=scope,
            job_id=job_id,
            first=job,
            seen=resume_version(last_event_id),
            settings=settings,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers=STREAM_HEADERS,
    )
