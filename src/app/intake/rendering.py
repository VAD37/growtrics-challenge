"""`LessonBrief` to `BriefBundle`: the files a worker is handed (D029, D061).

User text is data, never instruction. It is delivered inside fenced blocks in documents this
package authors, and the agent's directions live in the pinned template repo. Three defences
stack: the data sits inside a fence, the fence is labelled as data, and the surrounding document
is ours. Only the first is code, and it is `fence_for`.

The documents are files in a versioned pack (`templates/v1/`) rather than string literals in
Python, which is what makes a change to prompt wording a reviewable diff and what lets
`template_version` be folded into the brief hash.
"""

import json
from pathlib import Path
from string import Template
from typing import Final

from app.domain.brief import BriefBundle, BundleFile, LessonBrief
from app.domain.contracts import HARVEST_ROOT, OutputContract, contract_document
from app.domain.enums import ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.domain.records import BriefRecord
from app.intake.ports import TemplateSource
from app.intake.sealer import brief_from_record

BRIEF_FILE: Final[str] = "BRIEF.md"
CONTEXT_FILE: Final[str] = "CONTEXT.md"
CONSTRAINTS_FILE: Final[str] = "CONSTRAINTS.md"
OUTPUT_CONTRACT_FILE: Final[str] = "OUTPUT_CONTRACT.json"

_BRIEF_TEMPLATE: Final[str] = "BRIEF.md.tmpl"
_CONTEXT_TEMPLATE: Final[str] = "CONTEXT.md.tmpl"
_CONTEXT_ITEM_TEMPLATE: Final[str] = "CONTEXT_ITEM.md.tmpl"
_CONSTRAINTS_TEMPLATE: Final[str] = "CONSTRAINTS.md.tmpl"

MIN_FENCE_LENGTH: Final[int] = 3
_FENCE_CHAR: Final[str] = "`"

_TEMPLATE_ROOT: Final[Path] = Path(__file__).resolve().parent / "templates"
_NAME_ALLOWED: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

_NO_CONTEXT: Final[str] = "_No learner context: none supplied with this request._"


def fence_for(*texts: str) -> str:
    """Choose a fence longer than the longest backtick run in any of the data.

    This is the escaping, and it is escaping by construction rather than by substitution: a
    block opened with n+1 backticks cannot be closed by the n backticks inside it, and nothing
    in the learner's text has to be rewritten to make that true. Rewriting would change what the
    lesson is about, which is a worse failure than the one it prevents.
    """
    longest = 0
    for text in texts:
        run = 0
        for char in text:
            run = run + 1 if char == _FENCE_CHAR else 0
            longest = max(longest, run)
    return _FENCE_CHAR * max(MIN_FENCE_LENGTH, longest + 1)


class PackageTemplateSource:
    """Reads the template pack committed next to this module.

    The demo's `TemplateSource`. The eventual one is a git repo pinned by commit hash, which is
    why `template_version` is an argument rather than a path the caller assembles.
    """

    def __init__(self, root: Path = _TEMPLATE_ROOT) -> None:
        self.root: Path = root

    def read(self, template_version: str, name: str) -> str:
        """Read one template, rejecting anything that is not a plain name in a known pack.

        @audit both arguments are internal today and neither is checked anywhere else. The
        allowlist is here because a template version that ever becomes client-influenced would
        otherwise be a path traversal into the source tree, and the cost of preventing that now
        is two set comparisons.
        """
        for part, field in ((template_version, "template_version"), (name, "name")):
            if not part or not set(part) <= _NAME_ALLOWED or part.startswith("."):
                raise DomainError(
                    ErrorCode.INVALID_REQUEST,
                    {"field": field, "reason": "not_a_plain_name"},
                )
        path = self.root / template_version / name
        if not path.is_file():
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                {"field": "template", "reason": "unknown_template"},
            )
        return path.read_text(encoding="utf-8")


def _render(source: TemplateSource, version: str, name: str, values: dict[str, str]) -> str:
    """Substitute into one template.

    `Template.substitute` does not rescan what it inserted, so a learner writing `$instruction`
    inside their question cannot cause a second substitution. A missing placeholder raises
    rather than silently rendering `$name` into a document a worker will read.
    """
    return Template(source.read(version, name)).substitute(values)


def render_bundle(
    brief: LessonBrief,
    contract: OutputContract,
    *,
    source: TemplateSource | None = None,
) -> BriefBundle:
    """Render the four files a worker receives.

    One fence is computed across every piece of user text in the bundle, so the brief and the
    context documents share one convention and a reader never has to learn two.

    `contract` is the registry object, rendered here and read back by `custody.verifier` as the
    acceptance test (D077). It is a parameter rather than a lookup so the two paths cannot end
    up resolving different objects.
    """
    reader = source if source is not None else PackageTemplateSource()
    version = brief.template_version

    user_texts = [brief.instruction.text, *(item.text.text for item in brief.context)]
    fence = fence_for(*user_texts)

    brief_document = _render(
        reader,
        version,
        _BRIEF_TEMPLATE,
        {
            "fence": fence,
            "instruction": brief.instruction.text,
            "harvest_root": HARVEST_ROOT,
            "contract_file": OUTPUT_CONTRACT_FILE,
            "context_file": CONTEXT_FILE,
            "constraints_file": CONSTRAINTS_FILE,
        },
    )

    items = "\n".join(
        _render(
            reader,
            version,
            _CONTEXT_ITEM_TEMPLATE,
            {"kind": item.kind.value, "text": item.text.text, "fence": fence},
        )
        for item in brief.context
    )
    context_document = _render(
        reader,
        version,
        _CONTEXT_TEMPLATE,
        {"items": items if brief.context else _NO_CONTEXT},
    )

    lower, upper = contract.duration_bounds_s(brief.constraints.max_duration_s)
    constraints_document = _render(
        reader,
        version,
        _CONSTRAINTS_TEMPLATE,
        {
            "profile": brief.profile.value,
            "contract_version": contract.contract_version,
            "max_duration_s": str(brief.constraints.max_duration_s),
            "duration_cap_s": str(contract.max_duration_cap_s),
            "duration_lower_s": f"{lower:.1f}",
            "duration_upper_s": f"{upper:.1f}",
            "language": brief.constraints.language,
            "reading_level": (
                brief.constraints.reading_level.value
                if brief.constraints.reading_level is not None
                else "unspecified"
            ),
            "brief_file": BRIEF_FILE,
            "context_file": CONTEXT_FILE,
        },
    )

    contract_json = json.dumps(contract_document(contract), indent=2, sort_keys=True) + "\n"

    return BriefBundle(
        brief_id=brief.brief_id,
        template_version=version,
        brief_hash=brief.brief_hash,
        files=(
            BundleFile(path=BRIEF_FILE, text=brief_document),
            BundleFile(path=CONTEXT_FILE, text=context_document),
            BundleFile(path=CONSTRAINTS_FILE, text=constraints_document),
            BundleFile(path=OUTPUT_CONTRACT_FILE, text=contract_json),
        ),
    )


def bundle_for_record(
    record: BriefRecord,
    contract: OutputContract,
    *,
    profile: ProfileId,
    source: TemplateSource | None = None,
) -> BriefBundle:
    """Render the worker's file set from a stored `briefs` row.

    The seam between orchestration and generation carries the row rather than the sealed value
    (`orchestration/ports.py`), so this is the entry point the generation lane actually uses.
    `render_bundle` stays the one that renders, and this is one line of rehydration in front of
    it; keeping them separate is what makes the sealing path and the rendering path testable
    without each other.
    """
    return render_bundle(brief_from_record(record, profile=profile), contract, source=source)
