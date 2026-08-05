"""Output contracts: what must exist when generation finishes.

One object, two uses (D077). `contract_document` renders it into `OUTPUT_CONTRACT.json` in the
worker's workspace as the statement of what to make, and `custody.verifier` reads the same
object back as the acceptance test for what arrived. A worker that satisfies the file it was
handed passes verification by construction, and there is no second definition of "done" for
anybody to keep in step.

Server-owned. A client selects a `ProfileId` and never authors one of these: a client-authored
output contract lets the caller tell the validator what to accept, which is the same as having
no validator (`plan/14-api-schema.md`).

`video.short.v1` is transcribed from the table in `plan/14-api-schema.md`. `html.lesson.v1` is a
`ProfileId` member and is deliberately absent from `ENABLED_CONTRACTS` (D081): agent-authored
HTML is active content and every control in `plan/06-trust-boundary.md` was designed for inert
output, so asking for it is a rejection rather than a lookup miss.

Frozen slotted dataclasses rather than the pydantic models sketched in `plan/14-api-schema.md`.
`app/domain` carries no validation library; these values are authored here, never parsed from a
client, so there is no trust boundary for pydantic to guard.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from app.domain.enums import ArtifactRole, Audience, ProfileId
from app.domain.errors import DomainError, ErrorCode

MIB: Final[int] = 1024 * 1024
KIB: Final[int] = 1024

HARVEST_ROOT: Final[str] = "out"
"""The only directory a worker may write into and the only one custody reads from.

Named here rather than in the harvester because the worker is told it in `OUTPUT_CONTRACT.json`
and the path allowlist enforces it on the way back. Two readers, one constant.
"""


@dataclass(frozen=True, slots=True)
class ServingPolicy:
    """How the bytes may be served once verified.

    Part of the contract rather than an implementation detail, because "may this render in our
    origin" is a property of what the profile produces, not of the router that returns it.
    """

    origin: Literal["API", "ISOLATED"]
    disposition: Literal["inline", "attachment"]
    csp: str | None
    sandbox: bool
    nosniff: bool


@dataclass(frozen=True, slots=True)
class PartSpec:
    """One role a deliverable may contain, with its caps and its named checks."""

    role: ArtifactRole
    audience: Audience
    required: bool
    media_types: frozenset[str]
    max_bytes: int
    max_count: int
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OutputContract:
    """The complete statement of what a finished job must contain."""

    profile_id: ProfileId
    contract_version: str
    parts: tuple[PartSpec, ...]
    total_max_bytes: int
    max_file_count: int
    max_duration_cap_s: int
    duration_tolerance: float
    serving: ServingPolicy

    def duration_bounds_s(self, requested_max_duration_s: int) -> tuple[float, float]:
        """Accepted duration window for a request, cap first and tolerance second.

        The profile caps tighter than the request field allows (120s against 180s), so the cap
        is applied before the tolerance. Doing it the other way round would accept a 198s video
        for a request the profile never agreed to.
        """
        effective = min(requested_max_duration_s, self.max_duration_cap_s)
        return (
            effective * (1.0 - self.duration_tolerance),
            effective * (1.0 + self.duration_tolerance),
        )


VIDEO_SHORT_V1: Final[OutputContract] = OutputContract(
    profile_id=ProfileId.VIDEO_SHORT_V1,
    contract_version="v1",
    parts=(
        PartSpec(
            role=ArtifactRole.PRIMARY,
            audience=Audience.LEARNER,
            required=True,
            media_types=frozenset({"video/mp4"}),
            max_bytes=64 * MIB,
            max_count=1,
            checks=(
                "container_is_mp4",
                "video_stream_present",
                "audio_stream_present",
                # Two checks, not one: R7 asks for something that feels like a normal
                # educational video, and a silent AAC track satisfies the first while failing
                # the requirement.
                "audio_not_silent",
                "duration_within_bounds",
                "resolution_at_least_720p",
                "no_blank_frames",
            ),
        ),
        PartSpec(
            role=ArtifactRole.POSTER,
            audience=Audience.LEARNER,
            required=False,
            media_types=frozenset({"image/png", "image/jpeg"}),
            max_bytes=2 * MIB,
            max_count=1,
            checks=("image_decodes", "aspect_matches_video"),
        ),
        PartSpec(
            role=ArtifactRole.TRANSCRIPT,
            audience=Audience.LEARNER,
            required=False,
            media_types=frozenset({"text/plain"}),
            max_bytes=256 * KIB,
            max_count=1,
            checks=("utf8_decodes", "length_plausible_for_duration"),
        ),
        PartSpec(
            role=ArtifactRole.CAPTIONS,
            audience=Audience.LEARNER,
            required=False,
            media_types=frozenset({"text/vtt"}),
            max_bytes=256 * KIB,
            max_count=1,
            checks=("vtt_parses", "cue_times_within_duration"),
        ),
        PartSpec(
            role=ArtifactRole.LOG,
            audience=Audience.OPERATOR,
            required=False,
            media_types=frozenset({"application/x-ndjson"}),
            max_bytes=8 * MIB,
            max_count=4,
            checks=("utf8_decodes", "line_count_cap", "trace_id_present"),
        ),
    ),
    total_max_bytes=80 * MIB,
    max_file_count=24,
    max_duration_cap_s=120,
    duration_tolerance=0.10,
    serving=ServingPolicy(
        origin="API",
        disposition="inline",
        csp=None,
        sandbox=False,
        nosniff=True,
    ),
)
"""`plan/14-api-schema.md`, the `video.short.v1` table. Totals 80 MiB and 24 files."""


ENABLED_CONTRACTS: Final[Mapping[ProfileId, OutputContract]] = MappingProxyType(
    {ProfileId.VIDEO_SHORT_V1: VIDEO_SHORT_V1}
)
"""Every profile this build will actually make.

`html.lesson.v1` is a `ProfileId` member and is not here (D081). A profile is a registry entry,
a validator chain, and a template pack; a member of the enum on its own is a design, and a
design must not be reachable through an options field.
"""


def contract_for(profile: ProfileId) -> OutputContract:
    """Resolve a profile, or reject it.

    Returns the shared registry object rather than a copy: the identity is the point of D077,
    since the document the worker is handed and the acceptance test the validator runs have to
    be the same value.

    `plan/14-api-schema.md` gives this rejection its own code, `OUTPUT_PROFILE_NOT_SUPPORTED`
    (422). The demo catalog carries eight codes (`docs/demo.md`) and that is not one of them, so
    an unbuilt profile is `INVALID_REQUEST` naming the field, which is what a caller can act on.
    @TODO widen to `OUTPUT_PROFILE_NOT_SUPPORTED` when a second profile is built
    (`docs/plan/14-api-schema.md`).
    """
    contract = ENABLED_CONTRACTS.get(profile)
    if contract is None:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            {"field": "options.profile", "profile": profile.value},
        )
    return contract


def part_for_role(contract: OutputContract, role: ArtifactRole) -> PartSpec:
    """The part spec for a role, or a rejection naming the role the worker invented."""
    for part in contract.parts:
        if part.role is role:
            return part
    raise DomainError(
        ErrorCode.GENERATION_FAILED,
        {"reason": "role_not_in_contract", "role": role.value},
    )


def part_for_media_type(contract: OutputContract, media_type: str) -> PartSpec | None:
    """The part a media type belongs to, or `None`.

    Exact match, no case folding and no parameter stripping. A worker declaring `VIDEO/MP4` or
    `video/mp4; codecs=avc1` has not declared what the contract asked for, and a lenient
    comparison here is one more way for a declared type to disagree with the bytes.
    """
    for part in contract.parts:
        if media_type in part.media_types:
            return part
    return None


def contract_document(contract: OutputContract) -> Mapping[str, object]:
    """Render the contract as the JSON document a worker receives (D077).

    Pure: builds plain containers and does no I/O, so the domain stays free of both a
    serialisation library and a filesystem. `intake.rendering` writes the bytes.

    Media types are sorted because the source is a `frozenset`, and an unordered set would make
    two renders of one contract differ byte for byte, which would in turn move the brief hash
    that `template_version` is folded into (D061).
    """
    parts: list[Mapping[str, object]] = [
        {
            "role": part.role.value,
            "audience": part.audience.value,
            "required": part.required,
            "media_types": sorted(part.media_types),
            "max_bytes": part.max_bytes,
            "max_count": part.max_count,
            "checks": list(part.checks),
        }
        for part in contract.parts
    ]
    return {
        "profile_id": contract.profile_id.value,
        "contract_version": contract.contract_version,
        "harvest_root": HARVEST_ROOT,
        "parts": parts,
        "total_max_bytes": contract.total_max_bytes,
        "max_file_count": contract.max_file_count,
        "max_duration_cap_s": contract.max_duration_cap_s,
        "duration_tolerance": contract.duration_tolerance,
        "serving": {
            "origin": contract.serving.origin,
            "disposition": contract.serving.disposition,
            "csp": contract.serving.csp,
            "sandbox": contract.serving.sandbox,
            "nosniff": contract.serving.nosniff,
        },
    }
