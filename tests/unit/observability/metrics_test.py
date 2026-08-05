"""The metrics port and its no-op adapter.

The demo has no metrics table and no registry (D093, scope override item 8), so the only thing
worth asserting is that the port a caller codes against exists, that the no-op adapter satisfies
it, and that recording a metric can neither fail a request nor be awaited.
"""

import inspect
from typing import Final

import pytest

from app.observability.metrics import NO_LABELS, MetricsPort, NullMetrics

NAME: Final[str] = "job_submitted_total"
LABELS: Final[dict[str, str]] = {"profile": "video.short.v1"}


# --------------------------------------------------------------------------- the port


def test_the_null_adapter_satisfies_the_port() -> None:
    assert isinstance(NullMetrics(), MetricsPort)


def test_recording_is_not_awaitable() -> None:
    # A metric call sits inside a request path and a step body. Making it async would make an
    # observation an ordering constraint on the thing being observed; see the module docstring
    # for why the eventual SQL adapter buffers instead (D054).
    metrics = NullMetrics()
    for method in (metrics.counter, metrics.gauge, metrics.histogram):
        assert not inspect.iscoroutinefunction(method)


# --------------------------------------------------------------------------- the no-op


def test_counter_defaults_to_one() -> None:
    assert NullMetrics().counter(NAME) is None


def test_every_method_drops_its_value() -> None:
    metrics = NullMetrics()
    assert metrics.counter(NAME, 7, labels=LABELS) is None
    assert metrics.gauge("queue_depth", 3.0, labels=LABELS) is None
    assert metrics.histogram("stage_seconds", 1.5, labels=LABELS) is None


def test_the_no_op_keeps_no_registry() -> None:
    # Dropping is the whole implementation. A counter that accumulated in process would be a
    # second source of truth for numbers that D054 says are read out of SQL.
    metrics = NullMetrics()
    metrics.counter(NAME)
    assert not hasattr(metrics, "__dict__") or metrics.__dict__ == {}


def test_the_shared_empty_label_set_cannot_be_mutated() -> None:
    # One default label mapping is shared by every call site; a mutable one would let a label
    # written in a request leak into every later metric.
    with pytest.raises(TypeError):
        NO_LABELS["leaked"] = "value"  # type: ignore[index]
