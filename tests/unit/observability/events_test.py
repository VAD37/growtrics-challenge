"""The event vocabulary, and the sink that drops it.

The demo creates no `job_events` table (D093), so there is nothing here that asserts a row was
written. What is asserted is the shape the eventual writer takes and the two properties D041
buys: the emit site has to state the visibility, and operator detail is a field of its own
rather than something smuggled into `message`.
"""

from dataclasses import FrozenInstanceError
from typing import Final

import pytest

from app.domain.enums import StageName
from app.observability.events import (
    EMPTY_DETAIL,
    EventSeverity,
    EventSink,
    EventType,
    EventVisibility,
    JobEvent,
    NullEventSink,
)

JOB_ID: Final[str] = "job_0123456789ABCDEFGHJKMNPQRS"
TRACE_ID: Final[str] = "tr_0123456789ABCDEFGHJKMNPQRS"


def _event(**overrides: object) -> JobEvent:
    fields: dict[str, object] = {
        "job_id": JOB_ID,
        "type": EventType.STAGE_ENTERED,
        "stage": StageName.GENERATING,
        "attempt": 0,
        "severity": EventSeverity.INFO,
        "visibility": EventVisibility.USER,
        "message": "rendering",
        "trace_id": TRACE_ID,
    }
    fields.update(overrides)
    return JobEvent(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- vocabulary


def test_visibility_has_exactly_the_two_frozen_values() -> None:
    # `job_events.visibility` is `USER | OPERATOR` in the frozen DDL (plan/13-mvp.md).
    assert {member.value for member in EventVisibility} == {"USER", "OPERATOR"}


def test_severity_values_are_their_names() -> None:
    assert all(member.value == member.name for member in EventSeverity)


def test_event_type_values_are_their_names() -> None:
    assert all(member.value == member.name for member in EventType)


# --------------------------------------------------------------------------- the record


def test_event_is_a_frozen_value() -> None:
    event = _event()
    with pytest.raises(FrozenInstanceError):
        event.message = "other"  # type: ignore[misc]


def test_event_has_no_dict() -> None:
    assert not hasattr(_event(), "__dict__")


def test_visibility_has_no_default() -> None:
    # D041: the emit site knows the audience, so the type refuses to guess it. A default here
    # would make "who may read this" a thing somebody forgets rather than a thing they state.
    with pytest.raises(TypeError):
        JobEvent(  # type: ignore[call-arg]
            job_id=JOB_ID,
            type=EventType.STAGE_ENTERED,
            stage=StageName.GENERATING,
            attempt=0,
            severity=EventSeverity.INFO,
            message="rendering",
            trace_id=TRACE_ID,
        )


def test_detail_defaults_to_an_empty_mapping() -> None:
    assert _event().detail == {}


def test_the_default_detail_cannot_be_mutated_into_a_shared_one() -> None:
    with pytest.raises(TypeError):
        EMPTY_DETAIL["leaked"] = "value"  # type: ignore[index]


def test_detail_is_separate_from_message() -> None:
    # @audit `detail` is the field that never reaches a client (D041). It only stays that way
    # while it is a field: an operator note folded into `message` is serialised with it.
    event = _event(message="rendering", detail={"stderr": "worker path /tmp/x"})
    assert "stderr" not in event.message


# --------------------------------------------------------------------------- the null sink


async def test_the_null_sink_accepts_an_event_and_returns_nothing() -> None:
    assert await NullEventSink().emit(_event()) is None


def test_the_null_sink_satisfies_the_port() -> None:
    assert isinstance(NullEventSink(), EventSink)


def test_the_null_sink_keeps_nothing() -> None:
    # Not a buffer, not a registry, not a table. Dropping is the whole implementation until the
    # outbox writer exists (D051), and a test that says so stops it growing a store by accident.
    sink = NullEventSink()
    assert not hasattr(sink, "__dict__") or sink.__dict__ == {}
