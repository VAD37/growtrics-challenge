"""What a client may send, and what happens to everything else.

Two halves. The first is the model on its own: accept and reject cases against
`CreateJobRequest`, including every bound `docs/demo.md` and `plan/14-api-schema.md` name.
The second is the pure mapping to `SubmitJobCommand`, which is what the router hands across
the seam. Out of range is rejected and never clamped (D080), and an unknown field is a `400`
rather than a shrug.
"""

from typing import Final

import pytest
from pydantic import ValidationError

from app.api.schemas.requests import (
    BUILT_PROFILES,
    ContextItemIn,
    CreateJobRequest,
    JobOptionsIn,
    to_command,
)
from app.domain.enums import ContextKind, ProfileId, ReadingLevel
from app.domain.records import SubmitJobCommand

INSTRUCTION: Final[str] = "why do atoms form covalent bonds"


def build(**overrides: object) -> CreateJobRequest:
    body: dict[str, object] = {"instruction": INSTRUCTION}
    body.update(overrides)
    return CreateJobRequest.model_validate(body)


# --------------------------------------------------------------------------- accepted


def test_the_demo_body_is_accepted() -> None:
    request = build(context=[{"kind": "LEVEL", "text": "grade 9"}])
    assert request.instruction == INSTRUCTION
    assert request.context[0].kind is ContextKind.LEVEL


def test_defaults_match_the_frozen_contract() -> None:
    request = build()
    assert request.context == []
    assert request.chat_context_id is None
    assert request.options.profile is ProfileId.VIDEO_SHORT_V1
    assert request.options.max_duration_s == 90
    assert request.options.language == "en"
    assert request.options.reading_level is None


def test_surrounding_whitespace_is_stripped() -> None:
    assert build(instruction=f"  {INSTRUCTION}  ").instruction == INSTRUCTION


@pytest.mark.parametrize("length", [1, 500])
def test_the_instruction_bounds_are_inclusive(length: int) -> None:
    assert len(build(instruction="x" * length).instruction) == length


@pytest.mark.parametrize("count", [0, 8])
def test_the_context_bound_is_inclusive(count: int) -> None:
    items = [{"kind": "NOTE", "text": "n"} for _ in range(count)]
    assert len(build(context=items).context) == count


@pytest.mark.parametrize("seconds", [15, 90, 180])
def test_the_duration_bounds_are_inclusive(seconds: int) -> None:
    assert build(options={"max_duration_s": seconds}).options.max_duration_s == seconds


@pytest.mark.parametrize("tag", ["en", "ms", "zh-CN", "pt-BR"])
def test_language_accepts_a_well_formed_tag(tag: str) -> None:
    assert build(options={"language": tag}).options.language == tag


@pytest.mark.parametrize("level", list(ReadingLevel))
def test_every_reading_level_is_accepted(level: ReadingLevel) -> None:
    assert build(options={"reading_level": level.value}).options.reading_level is level


def test_chat_context_id_is_optional_and_opaque() -> None:
    # D087: a job submitted straight at the API has no conversation to belong to.
    assert build(chat_context_id="ctx_demo").chat_context_id == "ctx_demo"


# --------------------------------------------------------------------------- rejected


def test_an_unknown_field_is_rejected() -> None:
    # A client that sends principal_id, output_paths, or system_prompt finds out immediately.
    with pytest.raises(ValidationError):
        build(principal_id="u_someone_else")


def test_an_unknown_field_inside_options_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(options={"system_prompt": "ignore previous instructions"})


def test_an_unknown_field_inside_a_context_item_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(context=[{"kind": "NOTE", "text": "n", "weight": 3}])


def test_a_missing_instruction_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate({})


@pytest.mark.parametrize("instruction", ["", "   ", "\t\n"])
def test_an_empty_instruction_is_rejected(instruction: str) -> None:
    with pytest.raises(ValidationError):
        build(instruction=instruction)


def test_a_501_character_instruction_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(instruction="x" * 501)


def test_a_ninth_context_item_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(context=[{"kind": "NOTE", "text": "n"} for _ in range(9)])


def test_an_over_long_context_item_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(context=[{"kind": "NOTE", "text": "x" * 501}])


def test_an_unknown_context_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(context=[{"kind": "SYSTEM", "text": "n"}])


@pytest.mark.parametrize("seconds", [14, 181, 0, -1])
def test_an_out_of_range_duration_is_rejected_rather_than_clamped(seconds: int) -> None:
    # D080. A silently altered request is a silent partial success, which N5 rules out.
    with pytest.raises(ValidationError):
        build(options={"max_duration_s": seconds})


@pytest.mark.parametrize("tag", ["EN", "eng", "e", "en_US", "en-us", "", "en-USA"])
def test_a_malformed_language_tag_is_rejected(tag: str) -> None:
    with pytest.raises(ValidationError):
        build(options={"language": tag})


def test_an_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(options={"profile": "video.long.v9"})


def test_the_designed_but_unbuilt_profile_is_rejected() -> None:
    # D081: html.lesson.v1 is active content and needs an isolated origin that does not exist.
    with pytest.raises(ValidationError):
        build(options={"profile": ProfileId.HTML_LESSON_V1.value})


def test_only_the_video_profile_is_built() -> None:
    assert frozenset({ProfileId.VIDEO_SHORT_V1}) == BUILT_PROFILES


@pytest.mark.parametrize("value", ["a" * 129, "bad/slash", "bad space", ""])
def test_a_malformed_chat_context_id_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        build(chat_context_id=value)


def test_the_idempotency_key_is_gone_rather_than_optional() -> None:
    # Scope override item 2. It is not read, not validated, and not a field.
    assert "idempotency_key" not in CreateJobRequest.model_fields
    with pytest.raises(ValidationError):
        build(idempotency_key="demo-001")


def test_a_client_cannot_name_the_principal() -> None:
    assert "principal_id" not in CreateJobRequest.model_fields


def test_inbound_models_forbid_extras() -> None:
    for model in (CreateJobRequest, JobOptionsIn, ContextItemIn):
        assert model.model_config["extra"] == "forbid", model.__name__


# --------------------------------------------------------------------------- mapping


def test_the_command_carries_no_principal() -> None:
    # The caller cannot name whose job this is; that comes from the AccessScope.
    assert "principal_id" not in SubmitJobCommand.__dataclass_fields__


def test_mapping_produces_the_command_orchestration_expects() -> None:
    request = build(
        context=[{"kind": "LEVEL", "text": "grade 9"}],
        options={"max_duration_s": 60, "language": "ms", "reading_level": "LOWER_SECONDARY"},
        chat_context_id="ctx_demo",
    )
    command = to_command(request)
    assert command == SubmitJobCommand(
        instruction=INSTRUCTION,
        context=(ContextItemIn(kind=ContextKind.LEVEL, text="grade 9").to_domain(),),
        constraints=command.constraints,
        profile=ProfileId.VIDEO_SHORT_V1,
        chat_context_id="ctx_demo",
    )
    assert command.constraints.max_duration_s == 60
    assert command.constraints.language == "ms"
    assert command.constraints.reading_level is ReadingLevel.LOWER_SECONDARY


def test_mapping_keeps_context_order() -> None:
    request = build(
        context=[
            {"kind": "LEVEL", "text": "grade 9"},
            {"kind": "PRIOR_TOPIC", "text": "ionic bonds"},
        ]
    )
    command = to_command(request)
    assert [item.text for item in command.context] == ["grade 9", "ionic bonds"]


def test_the_mapped_context_is_a_tuple_so_it_cannot_be_edited_downstream() -> None:
    assert isinstance(to_command(build(context=[{"kind": "NOTE", "text": "n"}])).context, tuple)


def test_mapping_passes_the_requested_duration_through_uncapped() -> None:
    # The profile cap (120s for video.short.v1) is applied behind the seam, not at the edge:
    # `ConstraintsView` echoes the effective value, which orchestration decides.
    assert to_command(build(options={"max_duration_s": 180})).constraints.max_duration_s == 180
