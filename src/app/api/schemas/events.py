"""What one frame of `GET /v1/jobs/{job_id}/events` carries, and how it is framed.

Server-Sent Events, so the wire format is text: an optional `id:`, an `event:` name, one
`data:` line, and a blank line ending the block. The `data:` half is a JSON document built out
of the same models `JobView` is built out of -- `progress` is `ProgressView` verbatim, stage
and status are the same enums -- because a client that already renders a job document must not
need a second model to render the stream.

Two documents, because a stream ends for two different reasons and a client acts differently on
each. A job that reached a terminal status is finished, and reconnecting would open a
connection that answers once and closes again. A stream that hit its lifetime cap has more to
say and the client should come back. `reconnect` carries that answer directly, so a client
needs no table of reasons; a browser's `EventSource` reconnects on any close, which makes
`TERMINAL` the signal to call `close()` instead.

The event is a progress signal, not a second copy of the job. `failure` and `artifact` stay on
`GET /v1/jobs/{job_id}`, which a client reads once when the stream ends. Duplicating them here
would be the two job models this module exists to avoid.

`data:` has to be one line, and it is one for free: pydantic's JSON escapes every newline
inside a string, so a stage message can never break the framing and no code here has to strip
anything out of it.
"""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from app.api.schemas.common import ResponseDocument
from app.api.schemas.jobs import STAGE_STEP, ProgressView
from app.domain.enums import JobStatus, StageName
from app.domain.ids import JobId
from app.domain.records import JobRecord

__all__ = [
    "HEARTBEAT",
    "JOB_EVENT_KEYS",
    "RECONNECT_AFTER",
    "SSE_MEDIA_TYPE",
    "STREAM_END_KEYS",
    "EventName",
    "JobEventView",
    "StreamEndReason",
    "StreamEndView",
    "end_frame",
    "frame",
    "progress_frame",
]

type EventFrame = str
"""One complete SSE block, the terminating blank line included.

A distinct name from `str` because the framing is the invariant: anything handed to the
response body has already been through `frame`, and a bare string that skipped it would
silently corrupt the block that follows.
"""

SSE_MEDIA_TYPE: Final[str] = "text/event-stream"

HEARTBEAT: Final[EventFrame] = ": ping\n\n"
"""A comment line. Legal SSE, ignored by every client, and the only thing standing between an
idle connection and a proxy that drops it without telling either end."""

JOB_EVENT_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "job_id",
    "status",
    "stage",
    "progress",
    "version",
    "updated_at",
)
"""What the `data:` of a progress event serialises to, written down for the same reason
`JOB_DOCUMENT_KEYS` is: "what does this endpoint send" should be a list somebody can read."""

STREAM_END_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "job_id",
    "reason",
    "reconnect",
    "version",
)


class EventName(StrEnum):
    """The `event:` field. A client dispatches on it, so the set is closed and only grows."""

    PROGRESS = "progress"
    END = "end"


class StreamEndReason(StrEnum):
    """Why the server stopped talking.

    `GONE` covers the job disappearing under an open stream. It cannot happen in the demo,
    which deletes nothing, and it is here because the alternative is an exception raised after
    the response headers are already on the wire -- a truncated body with no explanation in it.
    """

    TERMINAL = "TERMINAL"
    MAX_DURATION = "MAX_DURATION"
    GONE = "GONE"


RECONNECT_AFTER: Final[Mapping[StreamEndReason, bool]] = MappingProxyType(
    {
        StreamEndReason.TERMINAL: False,
        StreamEndReason.MAX_DURATION: True,
        StreamEndReason.GONE: False,
    }
)
"""Should the client open another stream. Total over `StreamEndReason`, asserted so in the
tests, because a missing member here is a client that reconnects forever or never."""


class JobEventView(ResponseDocument):
    """One state of the job, sent because its `version` moved.

    `version` is on the document as well as in the SSE `id:`. The `id:` is what the browser
    replays as `Last-Event-ID` and a client never has to parse; the field is what a client that
    is not a browser compares, and reading it out of the body beats teaching every consumer
    where SSE keeps its ids.
    """

    job_id: JobId
    status: JobStatus
    stage: StageName
    progress: ProgressView
    version: int
    updated_at: datetime

    @classmethod
    def from_record(cls, job: JobRecord) -> JobEventView:
        """Pure mapping from the `jobs` row, doing no I/O, exactly like `JobView.from_record`."""
        step, message = STAGE_STEP[job.stage]
        return cls(
            job_id=job.job_id,
            status=job.status,
            stage=job.stage,
            progress=ProgressView(percent=job.progress_percent, step=step, message=message),
            version=job.version,
            updated_at=job.updated_at,
        )


class StreamEndView(ResponseDocument):
    """The last thing on the connection. `version` is the state the client is known to hold."""

    job_id: JobId
    reason: StreamEndReason
    reconnect: bool
    version: int


def frame(name: EventName, document: ResponseDocument, event_id: int | None = None) -> EventFrame:
    """Render one SSE block.

    `event_id` is the job's `version`, and it is deliberately absent on the control frames: a
    client resumes from the last state it actually saw, and pointing `Last-Event-ID` at a frame
    that carried no state would make the reconnect skip the state it is resuming to.
    """
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {name}")
    lines.append(f"data: {document.model_dump_json(by_alias=True)}")
    return "\n".join(lines) + "\n\n"


def progress_frame(job: JobRecord) -> EventFrame:
    return frame(EventName.PROGRESS, JobEventView.from_record(job), event_id=job.version)


def end_frame(job: JobRecord, reason: StreamEndReason) -> EventFrame:
    return frame(
        EventName.END,
        StreamEndView(
            job_id=job.job_id,
            reason=reason,
            reconnect=RECONNECT_AFTER[reason],
            version=job.version,
        ),
    )
