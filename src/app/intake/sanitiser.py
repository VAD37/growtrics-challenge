"""Layer 2 on the way in: normalise, strip, collapse, cap.

The only producer of `SanitisedText` (`plan/06-trust-boundary.md`). Every downstream signature
takes that type, so skipping this module is a type error rather than an oversight, and
`tests/unit/stages/sanitiser_test.py` asserts that no other module in `src/app` reaches past the
private mint.

What it does, and why each step is here rather than at the edge:

* **NFKC.** Compatibility forms are the cheapest way to write a word a keyword rule will not
  recognise: the fullwidth spelling of "ignore" (U+FF49 and friends) reads as `ignore` to a model
  and matches nothing a guard greps for. Normalising first means every later layer sees one
  spelling.
* **Control and format characters go.** Bidirectional overrides reorder what a reviewer reads
  without changing what a model reads, and zero-width characters split a word in the middle. Both
  are pure evasion; neither has a use in a chemistry question.
* **Whitespace collapses to single spaces.** A newline inside user text is what would let it
  close the renderer's fence or start a heading, so a cleaned string is one line by
  construction. The fence escaping in `rendering.py` is the second defence, not the only one.
* **Hard cap, with the original length kept.** A 900-character question cut to 500 is a fact the
  brief records rather than a difference nobody can see.

`@audit` this reduces injection risk and does not eliminate it. Homoglyphs survive: "atom" spelled
with a Cyrillic U+0430 normalises to itself under NFKC and still reads as `atom` to a person.
Nothing in this build stops that, because the guard keyword matching belongs to is cut from the demo
(`docs/demo.md`), so there is currently no rule for a homoglyph to evade. Restoring the guard
means restoring a confusables fold here as well. See `tests/redteam/intake_corpus_test.py`.
"""

import unicodedata
from typing import Final

from app.domain.brief import SanitisedContextItem, SanitisedText
from app.domain.errors import DomainError, ErrorCode
from app.domain.records import ContextItem

INSTRUCTION_MAX_CHARS: Final[int] = 500
"""The `CreateJobRequest.instruction` bound (`docs/demo.md`), repeated as the cleaner's cap.

The edge rejects a longer field outright (D080, no clamping); this cap catches text that grew
past the bound during normalisation and text that reached intake by any path other than the
HTTP edge.
"""

CONTEXT_ITEM_MAX_CHARS: Final[int] = 500

_SPACE: Final[str] = " "
_ASCII_WHITESPACE: Final[frozenset[str]] = frozenset("\t\n\r\v\f\x1c\x1d\x1e\x1f")
_SPACE_CATEGORIES: Final[frozenset[str]] = frozenset({"Zs", "Zl", "Zp"})
_DROPPED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
"""Control, format, private use, surrogate, unassigned. None of them is a chemistry question."""


def sanitise(raw: str, *, field: str, max_chars: int) -> SanitisedText:
    """Clean one untrusted string, or reject it naming the field it arrived in.

    `field` is carried into the error details rather than into a message: the catalog owns the
    words (D062), and a caller still needs to know which of eight strings was the problem.
    """
    if max_chars < 1:
        raise DomainError(ErrorCode.INVALID_REQUEST, {"field": field, "reason": "max_chars"})

    original_length = len(raw)
    normalised = unicodedata.normalize("NFKC", raw)

    kept: list[str] = []
    for char in normalised:
        if char in _ASCII_WHITESPACE:
            kept.append(_SPACE)
            continue
        category = unicodedata.category(char)
        if category in _SPACE_CATEGORIES:
            kept.append(_SPACE)
        elif category not in _DROPPED_CATEGORIES:
            kept.append(char)

    collapsed = _SPACE.join("".join(kept).split())
    if not collapsed:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            {"field": field, "reason": "empty_after_sanitisation"},
        )

    truncated = len(collapsed) > max_chars
    return SanitisedText._mint(
        collapsed[:max_chars],
        original_length=original_length,
        truncated=truncated,
    )


def sanitise_instruction(raw: str) -> SanitisedText:
    """The learner's question."""
    return sanitise(raw, field="instruction", max_chars=INSTRUCTION_MAX_CHARS)


def sanitise_context(items: tuple[ContextItem, ...]) -> tuple[SanitisedContextItem, ...]:
    """Clean each context item, keeping its kind and its position.

    `kind` is a closed enum and needs no cleaning, which is exactly why context arrives as
    labelled items rather than as free text: the label cannot be attacked, only the value can.
    """
    return tuple(
        SanitisedContextItem(
            kind=item.kind,
            text=sanitise(
                item.text, field=f"context[{index}].text", max_chars=CONTEXT_ITEM_MAX_CHARS
            ),
        )
        for index, item in enumerate(items)
    )
