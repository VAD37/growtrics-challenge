"""Job events: the vocabulary now, the sink later.

DEFERRED, STUB ONLY (scope override item 8, supersedes D051 and D054 for the demo). The demo
creates no `job_events` table (D093), serves no `GET /v1/jobs/{job_id}/events`, and therefore
keeps no event history at all. A reviewer sees a job's current state and nothing about how it
got there.

What the eventual implementation does, so picking this up is filling in a body:

* `SqlEventSink.emit` inserts one row into `job_events` (frozen DDL in `plan/13-mvp.md`:
  `job_id, seq, at, type, stage, attempt, severity, visibility, message, detail, trace_id`,
  primary key `(job_id, seq)`), through the transactional outbox in the **same transaction** as
  the state change that produced it (D051). A status write and its event cannot then diverge,
  which is the whole reason the outbox was promoted from deferred to built.
* `seq` is per job and gap-free, allocated inside that transaction, so the event feed has a
  cursor that does not depend on clock resolution.
* `visibility` is set here at the emit site, never by a filter downstream (D041). The emitter
  is the only code that knows whether a line is for the learner or for whoever is on call.
* `detail` is operator-only and is never serialised to a client. `plan/13-mvp.md` stage 6 keeps
  a test whose whole job is that assertion.

Until that exists, `NullEventSink` drops every event. The consequence, said out loud: a job that
fails tells a client `failure.code` and nothing more, and there is no record anywhere of which
stage it was in when it went wrong beyond `jobs.stage`.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from app.domain.enums import StageName
from app.domain.ids import JobId, TraceId


class EventVisibility(StrEnum):
    """Who a line is written for (D041). `job_events.visibility`, `USER | OPERATOR`.

    Not an audience for files -- that is `Audience` in `app.domain.enums`, which decides who may
    fetch an artifact. This decides who may read a sentence about a job.
    """

    USER = "USER"
    OPERATOR = "OPERATOR"


class EventSeverity(StrEnum):
    """`job_events.severity`. Frozen default is `INFO`."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class EventType(StrEnum):
    """`job_events.type`. The demo's four transitions, named before anything writes them.

    Members are added, never repurposed (D072). A reader that meets an unknown type tolerates
    it, which is why an event feed can grow new kinds without a contract version.
    """

    JOB_SUBMITTED = "JOB_SUBMITTED"
    STAGE_ENTERED = "STAGE_ENTERED"
    JOB_SUCCEEDED = "JOB_SUCCEEDED"
    JOB_FAILED = "JOB_FAILED"


EMPTY_DETAIL: Final[Mapping[str, object]] = MappingProxyType({})
"""The shared default for `JobEvent.detail`, read-only so no emit site can grow it in place."""


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One row `job_events` would hold, in process.

    Frozen and slotted like every record that crosses a seam (`app.domain.records`). It lives
    here rather than in `domain/` because nothing outside this package emits or consumes one
    while the sink is a stub, and moving it later is an import change.

    `visibility` deliberately has no default: D041 puts the choice at the emit site, and a
    default is how that choice becomes something a caller forgets to make.
    """

    job_id: JobId
    type: EventType
    stage: StageName
    attempt: int
    severity: EventSeverity
    visibility: EventVisibility
    message: str
    trace_id: TraceId
    detail: Mapping[str, object] = field(default=EMPTY_DETAIL)
    # @audit `detail` is untyped for the same reason `ArtifactRecord.probe` is: it is a jsonb
    # column whose keys differ per event type, nothing branches on its contents, and it is
    # operator-only. The rule that keeps it operator-only is that it stays a separate field --
    # an operator note folded into `message` is serialised to the client along with it (D041).


@runtime_checkable
class EventSink(Protocol):
    """Where an event goes. One implementation in the demo, and it goes nowhere."""

    async def emit(self, event: JobEvent) -> None:
        """Record one event.

        Async because the real sink writes SQL. It is called inside the transaction that made
        the state change, so the implementation takes its unit of work at construction rather
        than as an argument here (D051); the caller must not open a second transaction.
        """
        ...


class NullEventSink:
    """Drops every event (scope override item 8).

    Not a buffer and not an in-memory feed. An in-process history would be a second, shorter
    truth than the one D054 says gets read out of SQL, and it would disappear on restart while
    looking like it had not.
    """

    __slots__ = ()

    async def emit(self, event: JobEvent) -> None:
        # @TODO write through the outbox into `job_events` in the caller's transaction
        # (D051, D054, docs/plan/13-mvp.md stage 6). Deliberately not raising: emitting is not
        # supposed to be the thing that fails a job, so the demo path stays green with no feed.
        return None
