"""Metrics: the port now, the adapter later.

DEFERRED, STUB ONLY (scope override item 8). `docs/demo.md` cuts metrics from the demo
entirely, and D093 creates no table to put them in, so `NullMetrics` drops every measurement.

What the eventual implementation does:

* D054 keeps observation in Postgres: the adapter inserts rows, there is no console registry and
  no OpenTelemetry this round. A dashboard reading a second system is how a graph and an
  endpoint start disagreeing about the same number.
* `docs/demo.md` says the cheapest restoration reads metrics off `job_events` rather than a
  metrics table at all -- count events by type, measure stage duration from the gap between two
  `STAGE_ENTERED` rows. That path needs `job_events` (D093) and nothing else, which is why this
  module owns no schema of its own.
* The port stays synchronous whichever way that goes. A metric is recorded inside a request
  path and inside a step body; making it awaitable would put an observation on the critical
  path of the thing being observed, and a SQL adapter therefore buffers in process and flushes
  on its own schedule rather than writing a row per call.

The consequence, said out loud: nothing counts jobs, failures, or stage durations. Whether the
demo is healthy is answered by reading `jobs` directly.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

NO_LABELS: Final[Mapping[str, str]] = MappingProxyType({})
"""The shared default label set, read-only so one call site cannot mutate every other's."""


@runtime_checkable
class MetricsPort(Protocol):
    """Three instrument kinds, because a fourth is a naming argument rather than a measurement.

    `name` is the metric, `labels` are its dimensions. Neither is validated here: cardinality
    control belongs to whichever adapter has to store the result, and the stub stores nothing.
    """

    def counter(self, name: str, value: int = 1, *, labels: Mapping[str, str] = NO_LABELS) -> None:
        """Add to a monotonic total. Jobs submitted, failures by code."""
        ...

    def gauge(self, name: str, value: float, *, labels: Mapping[str, str] = NO_LABELS) -> None:
        """Record a level that goes both ways. Queue depth, active jobs per principal."""
        ...

    def histogram(self, name: str, value: float, *, labels: Mapping[str, str] = NO_LABELS) -> None:
        """Record a distribution sample. Stage duration in seconds, artifact size in bytes."""
        ...


class NullMetrics:
    """Drops every measurement (scope override item 8).

    Not a registry. An in-process counter would answer questions about one container since its
    last restart while looking like it answered them about the system.
    """

    __slots__ = ()

    def counter(self, name: str, value: int = 1, *, labels: Mapping[str, str] = NO_LABELS) -> None:
        # @TODO record it (D054; docs/demo.md "Metrics | read from `job_events`; nothing new").
        return None

    def gauge(self, name: str, value: float, *, labels: Mapping[str, str] = NO_LABELS) -> None:
        # @TODO as above.
        return None

    def histogram(self, name: str, value: float, *, labels: Mapping[str, str] = NO_LABELS) -> None:
        # @TODO as above.
        return None
