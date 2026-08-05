"""The versioned acceptance test (D064): our checks, over the bytes we received.

The chain a file runs is the `checks` tuple on its `PartSpec`, read from the same
`OutputContract` object that was rendered into the worker's `OUTPUT_CONTRACT.json` (D077). A
worker that satisfies the file it was handed passes here by construction, and there is no second
definition of "done" for the two to drift apart on.

What is real and what is deferred, stated plainly rather than left to be discovered:

| Check | State |
|-------|-------|
| `container_is_mp4` | implemented, reads the `ftyp` box |
| `utf8_decodes` | implemented |
| `line_count_cap` | implemented |
| everything else | stub, raises `NotImplementedError`, recorded as skipped |

`@audit` verification is therefore partial, and a `CLEAN` verdict from this build means "the
cheap structural checks passed", not "this is a lesson". A file can be a valid MP4 container
holding one black frame and no audio and still be `CLEAN` here, which is exactly the defect
`audio_not_silent` and `no_blank_frames` exist to catch. The verdict records how many checks
were skipped so the gap is visible on the row rather than only in this docstring.

The deferred checks need a media probe, which needs ffprobe behind the `MediaProbe` port
(`docs/plan/06-trust-boundary.md`, "Structural probe"). @TODO that probe, then fill in the six
stub bodies below; the registry, the chain, and the verdict logic do not change when they land.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain.artifact import CheckOutcome, HarvestedFile, VerifiedArtifact
from app.domain.contracts import OutputContract, PartSpec, part_for_role
from app.domain.enums import ScanVerdict
from app.domain.errors import DomainError, ErrorCode

VALIDATOR_VERSION: Final[str] = "custody.video.v1"
"""Recorded on every artifact (D064). Bump it whenever a check's meaning or set changes."""

LOG_LINE_CAP: Final[int] = 10_000
"""`line_count_cap` for `application/x-ndjson`. A log is evidence, not a payload."""

_MP4_BRAND_OFFSET: Final[int] = 4
_MP4_HEADER_MIN: Final[int] = 12
_DEFERRED_DETAIL: Final[str] = "deferred: needs a media probe (docs/plan/06-trust-boundary.md)"


@dataclass(frozen=True, slots=True)
class CheckInput:
    """Everything a check may look at. Notably absent: the worker's manifest.

    A check that could read a claim would be able to agree with it, and agreeing with the thing
    being verified is not verification.
    """

    data: bytes
    part: PartSpec
    contract: OutputContract
    requested_max_duration_s: int


CheckFn = Callable[[CheckInput], bool]
"""A named check. Returns whether it passed, or raises `NotImplementedError` if it is a stub."""


# --------------------------------------------------------------------------- implemented


def container_is_mp4(check: CheckInput) -> bool:
    """The `ftyp` box at offset 4, per ISO/IEC 14496-12.

    Cheap and worth having on its own: it is what separates a real container from a zip, a text
    file, or an HTML page wearing an `.mp4` name. It says nothing about what is inside.
    """
    data = check.data
    return (
        len(data) >= _MP4_HEADER_MIN and data[_MP4_BRAND_OFFSET : _MP4_BRAND_OFFSET + 4] == b"ftyp"
    )


def utf8_decodes(check: CheckInput) -> bool:
    """Strict UTF-8. A text artifact that is not text is not servable as one."""
    try:
        check.data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def line_count_cap(check: CheckInput) -> bool:
    return check.data.count(b"\n") <= LOG_LINE_CAP


# --------------------------------------------------------------------------- deferred


def _deferred(name: str) -> CheckFn:
    """Build a stub check that names what it needs.

    Deliberately a raise rather than a `return True`: the runner turns it into a skipped outcome
    with a reason, and any caller invoking a check directly gets the truth instead of a pass.
    """

    def check(_: CheckInput) -> bool:
        # @TODO implement `{name}` behind the `MediaProbe` port
        # (`docs/plan/06-trust-boundary.md`, "Structural probe"; `plan/03-module-layout.md`).
        # Until it exists this check is recorded as skipped on every artifact, and a verdict of
        # CLEAN is weaker than the contract's check list implies.
        raise NotImplementedError(name)

    check.__name__ = name
    return check


CHECKS: Final[Mapping[str, CheckFn]] = MappingProxyType(
    {
        "container_is_mp4": container_is_mp4,
        "utf8_decodes": utf8_decodes,
        "line_count_cap": line_count_cap,
        "video_stream_present": _deferred("video_stream_present"),
        "audio_stream_present": _deferred("audio_stream_present"),
        "audio_not_silent": _deferred("audio_not_silent"),
        "duration_within_bounds": _deferred("duration_within_bounds"),
        "resolution_at_least_720p": _deferred("resolution_at_least_720p"),
        "no_blank_frames": _deferred("no_blank_frames"),
        "image_decodes": _deferred("image_decodes"),
        "aspect_matches_video": _deferred("aspect_matches_video"),
        "length_plausible_for_duration": _deferred("length_plausible_for_duration"),
        "vtt_parses": _deferred("vtt_parses"),
        "cue_times_within_duration": _deferred("cue_times_within_duration"),
        "trace_id_present": _deferred("trace_id_present"),
    }
)
"""Every check name any enabled contract can ask for, asserted total by the tests."""


class ContractResultValidator:
    """Runs a part's named chain and records what it found.

    A check that raises `NotImplementedError` is recorded as skipped, not as passed and not as
    failed. Anything else raising is a bug in a check, and it is recorded as a failure rather
    than allowed to fail the whole harvest: one broken check must not turn a verified lesson
    into a lost one, and a quarantined artifact is inspectable.
    """

    version: str = VALIDATOR_VERSION

    def verify(
        self,
        harvested: HarvestedFile,
        contract: OutputContract,
        *,
        requested_max_duration_s: int,
    ) -> VerifiedArtifact:
        part = part_for_role(contract, harvested.descriptor.role)
        payload = CheckInput(
            data=harvested.data,
            part=part,
            contract=contract,
            requested_max_duration_s=requested_max_duration_s,
        )

        outcomes = tuple(self._run(name, payload) for name in part.checks)
        failed = tuple(item for item in outcomes if not item.passed and not item.skipped)
        skipped = tuple(item for item in outcomes if item.skipped)

        return VerifiedArtifact(
            harvested=harvested,
            verdict=ScanVerdict.QUARANTINED if failed else ScanVerdict.CLEAN,
            checks=outcomes,
            validator_version=self.version,
            probe={
                "size_bytes": harvested.size_bytes,
                "content_hash": harvested.content_hash,
                "media_type": harvested.descriptor.media_type,
                "rel_path": harvested.descriptor.rel_path,
                "validator_version": self.version,
                "checks_run": len(outcomes) - len(skipped),
                "checks_skipped": len(skipped),
                "worker_claim_agreed": harvested.claim_agreed,
            },
        )

    @staticmethod
    def _run(name: str, payload: CheckInput) -> CheckOutcome:
        check = CHECKS.get(name)
        if check is None:
            return CheckOutcome(name=name, passed=False, skipped=True, detail="unregistered")
        try:
            passed = check(payload)
        except NotImplementedError:
            return CheckOutcome(name=name, passed=False, skipped=True, detail=_DEFERRED_DETAIL)
        return CheckOutcome(name=name, passed=passed, skipped=False, detail="")


def require_complete(
    verified: tuple[VerifiedArtifact, ...],
    contract: OutputContract,
) -> None:
    """Contract match after verification: every required part present and clean.

    The ACL already refused a manifest that never declared a primary. This is the second half of
    the same question, asked after the bytes were read: a primary that arrived and failed its
    checks leaves the job just as undeliverable as one that never arrived, and the evidence is
    retained either way.

    `plan/14-api-schema.md` gives this failure its own code, `DELIVERABLE_INCOMPLETE`. The demo
    catalog has eight codes and that is not one of them, so it is `GENERATION_FAILED` with the
    reason in the details. @TODO widen the catalog when the deliverable endpoint is built.
    """
    for part in contract.parts:
        if not part.required:
            continue
        present = [item for item in verified if item.harvested.descriptor.role is part.role]
        if not present:
            raise DomainError(
                ErrorCode.GENERATION_FAILED,
                {"reason": "missing_required_part", "role": part.role.value},
            )
        if not any(item.verdict is ScanVerdict.CLEAN for item in present):
            raise DomainError(
                ErrorCode.GENERATION_FAILED,
                {"reason": "required_part_quarantined", "role": part.role.value},
            )
