"""The port surface itself: what crosses it, and which port is allowed to move a job.

Two kinds of check. The pydantic ones guard the trust boundary a worker's manifest crosses:
`ArtifactDescriptor` is the agent's claim about a file, so it is validated rather than believed.
The structural ones hold the ownership rule from `plan/12-data-control.md`: only orchestration
writes job status, and inside orchestration only one port method can.
"""

import typing
from dataclasses import MISSING, fields
from typing import Final, get_args, get_origin

import pytest
from pydantic import ValidationError

from app.domain.enums import ArtifactRole
from app.domain.records import JobRecord
from app.orchestration import ports
from app.orchestration.ports import (
    MAX_DESCRIPTORS,
    ArtifactDescriptor,
    GenerationOutcome,
    JobRepository,
    JobTransition,
    Submission,
    UnitOfWork,
)

SESSION_ID: Final[str] = "ses_" + "0" * 26


def _descriptor_kwargs() -> dict[str, object]:
    return {
        "role": ArtifactRole.PRIMARY,
        "media_type": "video/mp4",
        "size_bytes": 1024,
        "source_uri": "workspace://out/lesson.mp4",
    }


# --------------------------------------------------------------------------- trust boundary


def test_a_well_formed_descriptor_is_accepted() -> None:
    descriptor = ArtifactDescriptor(**_descriptor_kwargs())
    assert descriptor.role is ArtifactRole.PRIMARY
    assert descriptor.size_bytes == 1024


def test_a_descriptor_strips_whitespace_the_worker_left_behind() -> None:
    descriptor = ArtifactDescriptor(**{**_descriptor_kwargs(), "media_type": "  video/mp4  "})
    assert descriptor.media_type == "video/mp4"


def test_a_descriptor_is_frozen() -> None:
    descriptor = ArtifactDescriptor(**_descriptor_kwargs())
    with pytest.raises(ValidationError):
        descriptor.size_bytes = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("role", "SOMETHING_ELSE"),
        ("size_bytes", -1),
        ("media_type", ""),
        ("source_uri", ""),
        ("source_uri", "x" * 2000),
    ],
)
def test_a_descriptor_rejects_what_a_worker_should_not_send(field_name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ArtifactDescriptor(**{**_descriptor_kwargs(), field_name: value})


def test_a_descriptor_forbids_an_unknown_field() -> None:
    """A worker cannot smuggle a field custody has never heard of."""
    with pytest.raises(ValidationError):
        ArtifactDescriptor(**{**_descriptor_kwargs(), "storage_uri": "s3://ours/anything"})


def test_a_generation_outcome_rejects_a_forged_session_id() -> None:
    with pytest.raises(ValidationError):
        GenerationOutcome(session_id="../../etc/passwd", descriptors=())


def test_a_generation_outcome_caps_the_file_count() -> None:
    too_many = tuple(ArtifactDescriptor(**_descriptor_kwargs()) for _ in range(MAX_DESCRIPTORS + 1))
    with pytest.raises(ValidationError):
        GenerationOutcome(session_id=SESSION_ID, descriptors=too_many)


def test_a_generation_outcome_forbids_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        GenerationOutcome(
            session_id=SESSION_ID,
            descriptors=(),
            job_id="job_" + "0" * 26,
        )


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
