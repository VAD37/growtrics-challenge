"""The anti-corruption layer: an untrusted manifest in, domain descriptors or a code out.

Every rejection is asserted by its reason. A rejection that only asserted "it raised" would
pass while the ACL stopped a path traversal for the wrong reason, which is exactly how a
defence quietly stops working (D044).
"""

from collections.abc import Mapping
from typing import Final

import pytest

from app.domain.artifact import ArtifactDescriptor
from app.domain.contracts import contract_for
from app.domain.enums import ArtifactRole, Audience, ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.generation.acl import (
    MAX_PATH_DEPTH,
    accept_descriptors,
    parse_outcome,
)
from app.generation.ports import GenerationOutcome

SESSION_ID: Final[str] = "ses_" + "0" * 26
CONTRACT = contract_for(ProfileId.VIDEO_SHORT_V1)


def _payload(**overrides: object) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "session_id": SESSION_ID,
        "status": "COMPLETED",
        "descriptors": [
            {
                "role": "PRIMARY",
                "media_type": "video/mp4",
                "rel_path": "out/lesson.mp4",
                "size_bytes": 1024,
                "sha256": None,
            }
        ],
        "manifest": {
            "profile": "video.short.v1",
            "contract_version": "v1",
            "checks": [{"name": "container_is_mp4", "passed": True}],
            "duration_s": 88.0,
            "notes": None,
        },
    }
    payload.update(overrides)
    return payload


def _descriptor(**overrides: object) -> Mapping[str, object]:
    descriptor: dict[str, object] = {
        "role": "PRIMARY",
        "media_type": "video/mp4",
        "rel_path": "out/lesson.mp4",
        "size_bytes": 1024,
        "sha256": None,
    }
    descriptor.update(overrides)
    return descriptor


def _accept(payload: Mapping[str, object]) -> tuple[ArtifactDescriptor, ...]:
    return accept_descriptors(parse_outcome(payload), CONTRACT, expected_session_id=SESSION_ID)


def _rejection(payload: Mapping[str, object]) -> str:
    with pytest.raises(DomainError) as caught:
        _accept(payload)
    assert caught.value.code is ErrorCode.GENERATION_FAILED
    return caught.value.details["reason"]


# --------------------------------------------------------------------------- happy path


def test_a_well_formed_manifest_becomes_domain_descriptors() -> None:
    accepted = _accept(_payload())
    assert len(accepted) == 1
    primary = accepted[0]
    assert primary.role is ArtifactRole.PRIMARY
    assert primary.media_type == "video/mp4"
    assert primary.rel_path == "out/lesson.mp4"
    assert primary.declared_size_bytes == 1024


def test_audience_comes_from_the_contract_and_not_from_the_worker() -> None:
    """A worker cannot promote its own log to something a learner may fetch."""
    accepted = _accept(
        _payload(
            descriptors=[
                _descriptor(),
                _descriptor(
                    role="LOG",
                    media_type="application/x-ndjson",
                    rel_path="out/agent.jsonl",
                    size_bytes=200,
                ),
            ]
        )
    )
    audiences = {item.role: item.audience for item in accepted}
    assert audiences[ArtifactRole.PRIMARY] is Audience.LEARNER
    assert audiences[ArtifactRole.LOG] is Audience.OPERATOR


def test_outcome_survives_a_json_round_trip() -> None:
    outcome = parse_outcome(_payload())
    assert GenerationOutcome.model_validate_json(outcome.model_dump_json()) == outcome


# --------------------------------------------------------------------------- parsing


def test_an_unknown_field_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(DomainError) as caught:
        parse_outcome(_payload(storage_uri="s3://ours/anything"))
    assert caught.value.code is ErrorCode.GENERATION_FAILED
    assert caught.value.details["reason"] == "malformed_manifest"


def test_an_unknown_descriptor_field_is_rejected() -> None:
    with pytest.raises(DomainError):
        parse_outcome(_payload(descriptors=[_descriptor(scan_verdict="CLEAN")]))


def test_a_missing_manifest_is_rejected() -> None:
    payload = dict(_payload())
    del payload["manifest"]
    with pytest.raises(DomainError):
        parse_outcome(payload)


def test_a_malformed_sha256_claim_is_rejected() -> None:
    with pytest.raises(DomainError):
        parse_outcome(_payload(descriptors=[_descriptor(sha256="not-a-hash")]))


def test_a_negative_size_claim_is_rejected() -> None:
    with pytest.raises(DomainError):
        parse_outcome(_payload(descriptors=[_descriptor(size_bytes=-1)]))


# --------------------------------------------------------------------------- session


def test_a_manifest_for_another_session_is_rejected() -> None:
    assert _rejection(_payload(session_id="ses_" + "1" * 26)) == "session_mismatch"


def test_a_worker_reported_failure_is_a_rejection_with_its_own_reason() -> None:
    assert _rejection(_payload(status="FAILED")) == "worker_reported_failure"


def test_a_qualified_status_is_accepted_and_verified_like_any_other() -> None:
    accepted = _accept(_payload(status="QUALIFIED"))
    assert len(accepted) == 1


# --------------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    ("rel_path", "reason"),
    [
        ("/etc/passwd", "absolute_path"),
        ("/out/lesson.mp4", "absolute_path"),
        ("C:/windows/system32/config", "bad_component"),
        ("\\\\server\\share\\lesson.mp4", "bad_component"),
        ("out/../../../etc/passwd", "parent_traversal"),
        ("out/..", "parent_traversal"),
        ("../out/lesson.mp4", "parent_traversal"),
        ("out/./lesson.mp4", "bad_component"),
        ("out/.ssh/id_rsa", "bad_component"),
        ("~/lesson.mp4", "bad_component"),
        ("out/lesson.mp4\x00.txt", "bad_component"),
        ("out/les son.mp4", "bad_component"),
        ("out\\lesson.mp4", "bad_component"),
        ("tmp/lesson.mp4", "outside_harvest_root"),
        ("lesson.mp4", "outside_harvest_root"),
        ("out/", "bad_component"),
        ("out//lesson.mp4", "bad_component"),
        ("out/a/b/c/d/lesson.mp4", "path_too_deep"),
    ],
)
def test_every_hostile_path_is_rejected_by_its_own_reason(rel_path: str, reason: str) -> None:
    assert _rejection(_payload(descriptors=[_descriptor(rel_path=rel_path)])) == reason


def test_the_depth_bound_is_the_documented_one() -> None:
    assert MAX_PATH_DEPTH == 4
    deep = "out/" + "/".join(["a"] * (MAX_PATH_DEPTH - 1)) + "/lesson.mp4"
    assert _rejection(_payload(descriptors=[_descriptor(rel_path=deep)])) == "path_too_deep"


def test_a_nested_but_legal_path_is_accepted() -> None:
    accepted = _accept(_payload(descriptors=[_descriptor(rel_path="out/video/lesson.mp4")]))
    assert accepted[0].rel_path == "out/video/lesson.mp4"


def test_two_descriptors_cannot_claim_one_path() -> None:
    payload = _payload(
        descriptors=[
            _descriptor(),
            _descriptor(role="POSTER", media_type="image/png"),
        ]
    )
    assert _rejection(payload) == "duplicate_path"


# --------------------------------------------------------------------------- contract match


def test_a_role_outside_the_enum_is_rejected() -> None:
    assert _rejection(_payload(descriptors=[_descriptor(role="ROOT")])) == "unknown_role"


def test_a_role_outside_the_contract_is_rejected() -> None:
    payload = _payload(
        descriptors=[_descriptor(role="ASSET", media_type="image/png", rel_path="out/a.png")]
    )
    assert _rejection(payload) == "role_not_in_contract"


def test_a_media_type_outside_the_part_spec_is_rejected() -> None:
    payload = _payload(descriptors=[_descriptor(media_type="application/zip")])
    assert _rejection(payload) == "media_type_not_in_part"


def test_a_media_type_belonging_to_another_role_is_rejected() -> None:
    """`image/png` is a real contract type and it is not what a PRIMARY may be."""
    payload = _payload(descriptors=[_descriptor(media_type="image/png")])
    assert _rejection(payload) == "media_type_not_in_part"


def test_a_declared_size_over_the_part_cap_is_rejected() -> None:
    payload = _payload(descriptors=[_descriptor(size_bytes=CONTRACT.total_max_bytes)])
    assert _rejection(payload) == "declared_size_over_cap"


def test_a_declared_total_over_the_contract_cap_is_rejected() -> None:
    payload = _payload(
        descriptors=[
            _descriptor(size_bytes=60 * 1024 * 1024),
            _descriptor(
                role="LOG",
                media_type="application/x-ndjson",
                rel_path="out/a.jsonl",
                size_bytes=8 * 1024 * 1024,
            ),
            _descriptor(
                role="LOG",
                media_type="application/x-ndjson",
                rel_path="out/b.jsonl",
                size_bytes=8 * 1024 * 1024,
            ),
            _descriptor(
                role="LOG",
                media_type="application/x-ndjson",
                rel_path="out/c.jsonl",
                size_bytes=8 * 1024 * 1024,
            ),
        ]
    )
    assert _rejection(payload) == "declared_total_over_cap"


def test_more_files_in_one_role_than_the_part_allows_is_rejected() -> None:
    payload = _payload(
        descriptors=[
            _descriptor(),
            _descriptor(rel_path="out/lesson2.mp4"),
        ]
    )
    assert _rejection(payload) == "too_many_for_role"


def test_more_files_than_the_contract_allows_is_rejected() -> None:
    payload = _payload(
        descriptors=[
            _descriptor(
                role="LOG",
                media_type="application/x-ndjson",
                rel_path=f"out/log{index}.jsonl",
                size_bytes=10,
            )
            for index in range(CONTRACT.max_file_count + 1)
        ]
    )
    assert _rejection(payload) == "too_many_files"


def test_a_manifest_with_no_primary_is_rejected() -> None:
    payload = _payload(
        descriptors=[
            _descriptor(
                role="TRANSCRIPT",
                media_type="text/plain",
                rel_path="out/transcript.txt",
                size_bytes=64,
            )
        ]
    )
    assert _rejection(payload) == "missing_required_part"


def test_an_empty_manifest_is_rejected() -> None:
    assert _rejection(_payload(descriptors=[])) == "missing_required_part"


def test_optional_parts_are_accepted_alongside_the_primary() -> None:
    accepted = _accept(
        _payload(
            descriptors=[
                _descriptor(),
                _descriptor(role="POSTER", media_type="image/png", rel_path="out/poster.png"),
                _descriptor(
                    role="TRANSCRIPT",
                    media_type="text/plain",
                    rel_path="out/transcript.txt",
                    size_bytes=64,
                ),
            ]
        )
    )
    assert {item.role for item in accepted} == {
        ArtifactRole.PRIMARY,
        ArtifactRole.POSTER,
        ArtifactRole.TRANSCRIPT,
    }
