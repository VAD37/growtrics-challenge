"""Layer 2 of the way in (`plan/06-trust-boundary.md`): the only producer of `SanitisedText`."""

from typing import Final

import pytest

from app.domain.brief import SanitisedText
from app.domain.enums import ContextKind
from app.domain.errors import DomainError, ErrorCode
from app.domain.records import ContextItem
from app.intake.sanitiser import (
    CONTEXT_ITEM_MAX_CHARS,
    INSTRUCTION_MAX_CHARS,
    sanitise,
    sanitise_context,
    sanitise_instruction,
)

# Written as escapes rather than as characters: invisible in a diff is exactly the property
# these carry into an attack, and a reviewer should not have to trust that a blank-looking byte
# is the one the test name claims.
BIDI_OVERRIDE: Final[str] = "\u202e"
ZERO_WIDTH_SPACE: Final[str] = "\u200b"
ZERO_WIDTH_JOINER: Final[str] = "\u200d"
BYTE_ORDER_MARK: Final[str] = "\ufeff"
FULLWIDTH_IGNORE: Final[str] = "\uff49\uff47\uff4e\uff4f\uff52\uff45"
NO_BREAK_SPACE: Final[str] = "\u00a0"
IDEOGRAPHIC_SPACE: Final[str] = "\u3000"
LINE_SEPARATOR: Final[str] = "\u2028"


def test_returns_sanitised_text_rather_than_a_string() -> None:
    result = sanitise("covalent bonds", field="instruction", max_chars=INSTRUCTION_MAX_CHARS)
    assert isinstance(result, SanitisedText)
    assert result.text == "covalent bonds"
    assert result.original_length == len("covalent bonds")
    assert result.truncated is False


def test_nfkc_normalises_compatibility_forms() -> None:
    """Fullwidth text is the cheapest way to write a word a keyword rule will not recognise."""
    result = sanitise(FULLWIDTH_IGNORE, field="f", max_chars=100)
    assert result.text == "ignore"


def test_control_and_format_characters_are_stripped() -> None:
    raw = f"bo{ZERO_WIDTH_SPACE}nd{BIDI_OVERRIDE}ing{BYTE_ORDER_MARK}\x07"
    result = sanitise(raw, field="f", max_chars=100)
    assert result.text == "bonding"
    for stripped in (ZERO_WIDTH_SPACE, BIDI_OVERRIDE, BYTE_ORDER_MARK, "\x07"):
        assert stripped not in result.text


def test_zero_width_joiner_cannot_hide_inside_a_word() -> None:
    result = sanitise(f"ig{ZERO_WIDTH_JOINER}nore previous", field="f", max_chars=100)
    assert result.text == "ignore previous"


def test_whitespace_of_every_kind_collapses_to_one_space() -> None:
    raw = f"  why\t\tdo\n\n\natoms{NO_BREAK_SPACE}{IDEOGRAPHIC_SPACE}bond {LINE_SEPARATOR} now  "
    result = sanitise(raw, field="f", max_chars=100)
    assert result.text == "why do atoms bond now"


def test_newlines_cannot_survive_into_a_fenced_block() -> None:
    """A single line in means a single line out, which is what the renderer's fence relies on."""
    result = sanitise("first\n```\n## heading", field="f", max_chars=200)
    assert "\n" not in result.text
    assert result.text == "first ``` ## heading"


def test_cap_truncates_and_records_the_original_length() -> None:
    raw = "a" * 900
    result = sanitise(raw, field="instruction", max_chars=INSTRUCTION_MAX_CHARS)
    assert len(result.text) == INSTRUCTION_MAX_CHARS
    assert result.original_length == 900
    assert result.truncated is True


def test_truncation_is_false_when_only_whitespace_was_removed() -> None:
    result = sanitise("  spaced   out  ", field="f", max_chars=100)
    assert result.truncated is False
    assert result.original_length == len("  spaced   out  ")


def test_empty_after_cleaning_is_rejected_by_field_name() -> None:
    with pytest.raises(DomainError) as caught:
        sanitise(f"{ZERO_WIDTH_SPACE}{BIDI_OVERRIDE}  ", field="instruction", max_chars=100)
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert caught.value.details["field"] == "instruction"
    assert caught.value.details["reason"] == "empty_after_sanitisation"


def test_zero_or_negative_cap_is_a_caller_bug() -> None:
    with pytest.raises(DomainError):
        sanitise("text", field="f", max_chars=0)


def test_instruction_helper_uses_the_request_bound() -> None:
    assert INSTRUCTION_MAX_CHARS == 500
    assert CONTEXT_ITEM_MAX_CHARS == 500
    result = sanitise_instruction("why do atoms form covalent bonds")
    assert result.text == "why do atoms form covalent bonds"


def test_context_items_keep_their_kind_and_order() -> None:
    items = (
        ContextItem(kind=ContextKind.LEVEL, text=" grade 9 "),
        ContextItem(kind=ContextKind.PRIOR_TOPIC, text=f"ionic{ZERO_WIDTH_SPACE} bonding"),
    )
    cleaned = sanitise_context(items)
    assert tuple(item.kind for item in cleaned) == (ContextKind.LEVEL, ContextKind.PRIOR_TOPIC)
    assert [item.text.text for item in cleaned] == ["grade 9", "ionic bonding"]


def test_context_item_rejection_names_its_index() -> None:
    items = (
        ContextItem(kind=ContextKind.LEVEL, text="grade 9"),
        ContextItem(kind=ContextKind.NOTE, text=ZERO_WIDTH_SPACE),
    )
    with pytest.raises(DomainError) as caught:
        sanitise_context(items)
    assert caught.value.details["field"] == "context[1].text"


def test_sanitiser_is_the_only_module_that_mints_sanitised_text() -> None:
    """The mint is private by name; this asserts nobody in `src/` reached past the underscore.

    `SanitisedText` is worth nothing as a guarantee if a second module can produce one, and the
    underscore is a convention a compiler does not enforce. This is the enforcement.
    """
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[3] / "src" / "app"
    callers = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if "SanitisedText._mint(" in path.read_text(encoding="utf-8")
    }
    assert callers == {"intake/sanitiser.py"}
