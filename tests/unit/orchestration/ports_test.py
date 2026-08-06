"""The port surface itself: what crosses it, and which port is allowed to move a job.

Two kinds of check. The first holds the seam's one rule about a worker's answer: it crosses
unopened. `GenerationOutcome` carries the raw manifest and no parsed view of it, so there is
exactly one place that decides what a worker may claim -- `generation.acl`, reached from
`custody.harvester` where the bytes are pulled -- rather than one on the way through
orchestration and another inside custody. The rest hold the ownership rule from
`plan/12-data-control.md`: only orchestration writes job status, and inside orchestration only
one port method can.
"""

import typing
from dataclasses import MISSING, fields, is_dataclass
from typing import Final, get_args, get_origin

from app.domain.records import JobRecord
from app.orchestration import ports
from app.orchestration.ports import (
    GenerationOutcome,
    JobRepository,
    JobTransition,
    Submission,
    UnitOfWork,
)

SESSION_ID: Final[str] = "ses_" + "0" * 26


# --------------------------------------------------------------------------- trust boundary


def test_a_generation_outcome_is_a_session_and_a_document() -> None:
    outcome = GenerationOutcome(session_id=SESSION_ID, document={"status": "COMPLETED"})
    assert outcome.session_id == SESSION_ID
    assert outcome.document == {"status": "COMPLETED"}


def test_a_generation_outcome_is_frozen() -> None:
    """What crossed is what is carried. Nothing may edit a worker's answer in flight."""
    outcome = GenerationOutcome(session_id=SESSION_ID, document={})
    assert is_dataclass(outcome)
    try:
        outcome.session_id = "ses_" + "1" * 26  # type: ignore[misc]
    except Exception as error:
        assert isinstance(error, AttributeError | TypeError)
    else:  # pragma: no cover - a mutable outcome is the failure this test exists for
        raise AssertionError("GenerationOutcome must be frozen")


def test_the_outcome_carries_no_parsed_view_of_the_document() -> None:
    """The regression this file exists to catch.

    A `descriptors` field here would mean the manifest was accepted once on the way through
    orchestration and re-read once inside custody, which is two definitions of what a worker is
    allowed to claim and one of them outside the anti-corruption layer.
    """
    assert {field.name for field in fields(GenerationOutcome)} == {"session_id", "document"}


# --------------------------------------------------------------------------- ownership


def _protocols() -> dict[str, type]:
    """The ports this module declares, without `typing.Protocol` itself, which it imports."""
    return {
        name: member
        for name, member in vars(ports).items()
        if isinstance(member, type)
        and getattr(member, "_is_protocol", False)
        and member.__module__ == ports.__name__
    }


def _method_hints(protocol: type, name: str) -> tuple[object, ...]:
    method = getattr(protocol, name)
    hints = typing.get_type_hints(method, vars(ports))
    return tuple(hints.values())


def _mentions(annotation: object, target: type) -> bool:
    """Does an annotation name a type anywhere inside it, including `Page[X]` and `X | None`."""
    if annotation is target:
        return True
    if get_origin(annotation) is None:
        return False
    return any(_mentions(argument, target) for argument in get_args(annotation))


def _methods(protocol: type) -> list[str]:
    return [
        name
        for name, member in vars(protocol).items()
        if not name.startswith("_") and callable(member)
    ]


def test_the_port_surface_is_the_ten_the_seam_names() -> None:
    assert set(_protocols()) == {
        "AdmissionPolicy",
        "ArtifactReader",
        "ArtifactWriter",
        "BriefWriter",
        "Clock",
        "GenerationGateway",
        "JobRepository",
        "RequestStore",
        "UnitOfWork",
        "WorkQueue",
    }


def test_only_the_job_repository_can_move_a_job() -> None:
    """`JobTransition` is the only shape that changes a `jobs` row, and one method takes it.

    This is the enforceable half of "only orchestration writes job status"
    (`plan/12-data-control.md`, D066). No other lane's port can express the write.
    """
    writers = [
        (protocol_name, method_name)
        for protocol_name, protocol in _protocols().items()
        for method_name in _methods(protocol)
        if any(_mentions(hint, JobTransition) for hint in _method_hints(protocol, method_name))
    ]
    assert writers == [("JobRepository", "apply_transition")]


def test_no_other_port_even_sees_a_job_record() -> None:
    """Custody and generation get a `job_id`, never the row.

    A port that cannot see a status is a port that cannot be talked into writing one.
    """
    leaks = [
        (protocol_name, method_name)
        for protocol_name, protocol in _protocols().items()
        if protocol_name != "JobRepository"
        for method_name in _methods(protocol)
        if any(_mentions(hint, JobRecord) for hint in _method_hints(protocol, method_name))
    ]
    assert leaks == []


def test_a_job_repository_read_takes_a_scope_first() -> None:
    """D067 survives the scope override: the argument stays even though the check is gone."""
    import inspect

    for name in ("count_active", "get", "list"):
        parameters = list(inspect.signature(getattr(JobRepository, name)).parameters)
        assert parameters[:2] == ["self", "scope"], name


# --------------------------------------------------------------------------- the submit write


def test_a_submission_cannot_be_built_without_all_three_rows() -> None:
    """Writing the job row without the request row is not a mistake anyone can make here."""
    assert {field.name for field in fields(Submission)} == {"request", "job", "work_item"}
    for field in fields(Submission):
        assert field.default is MISSING, field.name
        assert field.default_factory is MISSING, field.name


def test_the_unit_of_work_offers_exactly_one_write() -> None:
    assert _methods(UnitOfWork) == ["commit_submission"]


def test_a_transition_carries_no_free_text() -> None:
    """A failure message comes from `ERROR_CATALOG`, never from a raise site (D062)."""
    assert "message" not in {field.name for field in fields(JobTransition)}
