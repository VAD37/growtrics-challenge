"""The output contract registry: one object, two uses (D077).

The interesting assertions here are the identity ones. `video.short.v1` is rendered into the
worker's `OUTPUT_CONTRACT.json` and then read back by the validator as the acceptance test, and
the entire safety argument is that those are the same object rather than two documents somebody
keeps in step by hand.
"""

import json
from dataclasses import FrozenInstanceError, is_dataclass
from typing import Final

import pytest

from app.domain.contracts import (
    ENABLED_CONTRACTS,
    VIDEO_SHORT_V1,
    OutputContract,
    PartSpec,
    ServingPolicy,
    contract_document,
    contract_for,
    part_for_media_type,
    part_for_role,
)
from app.domain.enums import ArtifactRole, Audience, ProfileId
from app.domain.errors import DomainError, ErrorCode

MIB: Final[int] = 1024 * 1024


def test_video_short_v1_matches_the_tabulated_contract() -> None:
    """`plan/14-api-schema.md`, the `video.short.v1` table, transcribed and asserted."""
    contract = contract_for(ProfileId.VIDEO_SHORT_V1)

    assert contract.contract_version == "v1"
    assert contract.total_max_bytes == 80 * MIB
    assert contract.max_file_count == 24
    assert contract.duration_tolerance == 0.10
    assert contract.max_duration_cap_s == 120

    roles = tuple(part.role for part in contract.parts)
    assert roles == (
        ArtifactRole.PRIMARY,
        ArtifactRole.POSTER,
        ArtifactRole.TRANSCRIPT,
        ArtifactRole.CAPTIONS,
        ArtifactRole.LOG,
    )

    primary = part_for_role(contract, ArtifactRole.PRIMARY)
    assert primary.required is True
    assert primary.audience is Audience.LEARNER
    assert primary.media_types == frozenset({"video/mp4"})
    assert primary.max_bytes == 64 * MIB
    assert primary.checks == (
        "container_is_mp4",
        "video_stream_present",
        "audio_stream_present",
        "audio_not_silent",
        "duration_within_bounds",
        "resolution_at_least_720p",
        "no_blank_frames",
    )


def test_only_the_log_part_is_operator_audience() -> None:
    """A harvested agent log can be CLEAN and still be nothing a learner may fetch (D078)."""
    contract = contract_for(ProfileId.VIDEO_SHORT_V1)
    operator_roles = {part.role for part in contract.parts if part.audience is Audience.OPERATOR}
    assert operator_roles == {ArtifactRole.LOG}


def test_no_part_may_exceed_the_total_cap() -> None:
    contract = contract_for(ProfileId.VIDEO_SHORT_V1)
    for part in contract.parts:
        assert part.max_bytes <= contract.total_max_bytes


def test_registry_returns_one_shared_object() -> None:
    """Two lookups are the same object, so the worker's copy and the test cannot drift."""
    assert contract_for(ProfileId.VIDEO_SHORT_V1) is VIDEO_SHORT_V1
    assert contract_for(ProfileId.VIDEO_SHORT_V1) is contract_for(ProfileId.VIDEO_SHORT_V1)


def test_html_lesson_v1_is_a_profile_member_and_not_enabled() -> None:
    """Designed, not built (D081). Asking for it is a rejection, not a KeyError."""
    assert ProfileId.HTML_LESSON_V1 in set(ProfileId)
    assert ProfileId.HTML_LESSON_V1 not in ENABLED_CONTRACTS

    with pytest.raises(DomainError) as caught:
        contract_for(ProfileId.HTML_LESSON_V1)
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert caught.value.details["profile"] == ProfileId.HTML_LESSON_V1.value


def test_contract_types_are_frozen_dataclasses() -> None:
    for record in (OutputContract, PartSpec, ServingPolicy):
        assert is_dataclass(record)

    with pytest.raises(FrozenInstanceError):
        VIDEO_SHORT_V1.contract_version = "v2"  # type: ignore[misc]


def test_part_lookup_by_media_type_is_exact() -> None:
    contract = contract_for(ProfileId.VIDEO_SHORT_V1)
    assert part_for_media_type(contract, "video/mp4").role is ArtifactRole.PRIMARY
    assert part_for_media_type(contract, "image/jpeg").role is ArtifactRole.POSTER
    assert part_for_media_type(contract, "video/quicktime") is None
    assert part_for_media_type(contract, "VIDEO/MP4") is None


def test_missing_role_lookup_is_a_domain_error() -> None:
    with pytest.raises(DomainError) as caught:
        part_for_role(contract_for(ProfileId.VIDEO_SHORT_V1), ArtifactRole.ASSET)
    assert caught.value.code is ErrorCode.GENERATION_FAILED


def test_duration_bounds_apply_the_cap_before_the_tolerance() -> None:
    contract = contract_for(ProfileId.VIDEO_SHORT_V1)

    lower, upper = contract.duration_bounds_s(90)
    assert (round(lower, 3), round(upper, 3)) == (81.0, 99.0)

    # 180 is inside the request field's range and outside the profile's cap.
    capped_lower, capped_upper = contract.duration_bounds_s(180)
    assert (round(capped_lower, 3), round(capped_upper, 3)) == (108.0, 132.0)


def test_contract_document_is_json_safe_and_complete() -> None:
    """What the worker is handed. Every cap it must satisfy has to survive the round trip."""
    document = contract_document(VIDEO_SHORT_V1)
    reloaded = json.loads(json.dumps(document))

    assert reloaded["profile_id"] == "video.short.v1"
    assert reloaded["contract_version"] == "v1"
    assert reloaded["total_max_bytes"] == 80 * MIB
    assert reloaded["max_file_count"] == 24
    assert reloaded["harvest_root"] == "out"
    assert reloaded["serving"]["nosniff"] is True

    primary = next(part for part in reloaded["parts"] if part["role"] == "PRIMARY")
    assert primary["media_types"] == ["video/mp4"]
    assert primary["max_bytes"] == 64 * MIB
    assert primary["required"] is True
    assert "container_is_mp4" in primary["checks"]

    # Media types are sorted, so two renders of one contract are byte-identical.
    poster = next(part for part in reloaded["parts"] if part["role"] == "POSTER")
    assert poster["media_types"] == ["image/jpeg", "image/png"]


def test_contract_document_reflects_the_registry_object_it_was_given() -> None:
    """The rendering path reads the same object the validator does, not a copy of the numbers."""
    contract = contract_for(ProfileId.VIDEO_SHORT_V1)
    document = contract_document(contract)

    for part, rendered in zip(contract.parts, document["parts"], strict=True):  # type: ignore[arg-type]
        assert rendered["role"] == part.role.value
        assert rendered["max_bytes"] == part.max_bytes
        assert rendered["max_count"] == part.max_count
        assert tuple(rendered["checks"]) == part.checks
