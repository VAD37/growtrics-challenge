"""The artifact summary: enough to render a card, and nothing custody keeps to itself.

`ArtifactSummaryView` is the one artifact shape the demo serves. It is embedded in `JobView`
and it is the item type of `GET /v1/artifacts`, so what it leaks it leaks in two places.
`storage_uri`, `content_hash`, `scan_verdict`, `probe`, and `validator_version` are custody's
and stay behind the API.
"""

from dataclasses import replace

import pytest
from api_fakes import make_artifact

from app.api.schemas.artifacts import (
    ARTIFACT_SUMMARY_KEYS,
    ArtifactSummaryView,
    content_url_for,
)
from app.domain.enums import ArtifactRole


def render(**overrides: object) -> dict[str, object]:
    record = make_artifact()
    if overrides:
        record = replace(record, **overrides)
    return ArtifactSummaryView.from_record(record).model_dump(mode="json", by_alias=True)


def test_the_summary_has_exactly_the_documented_keys() -> None:
    assert set(render()) == set(ARTIFACT_SUMMARY_KEYS)


@pytest.mark.parametrize(
    "leaked", ["storage_uri", "content_hash", "scan_verdict", "probe", "validator_version"]
)
def test_custody_internals_do_not_reach_a_client(leaked: str) -> None:
    assert leaked not in render()


def test_the_principal_of_another_learner_is_not_on_the_card() -> None:
    assert "principal_id" not in render()


def test_media_type_comes_from_the_mime_column() -> None:
    # The column is `mime`; the wire name is `media_type` and has been since 14-api-schema.
    assert render()["media_type"] == "video/mp4"


def test_the_content_url_is_the_only_way_to_the_bytes() -> None:
    record = make_artifact()
    assert render()["content_url"] == content_url_for(record.artifact_id)
    assert render()["content_url"] == f"/v1/artifacts/{record.artifact_id}/content"


def test_the_duration_comes_out_of_the_probe() -> None:
    assert render()["duration_s"] == 87.5


def test_a_probe_without_a_duration_gives_null_rather_than_a_guess() -> None:
    assert render(probe={})["duration_s"] is None


@pytest.mark.parametrize(
    "bad", [{"duration_s": "87.5"}, {"duration_s": None}, {"duration_s": True}]
)
def test_a_probe_duration_that_is_not_a_number_gives_null(bad: dict[str, object]) -> None:
    # The probe blob is jsonb written by custody; the edge reads it defensively rather than
    # trusting a shape nothing validates.
    assert render(probe=bad)["duration_s"] is None


def test_the_poster_url_is_null_in_the_demo() -> None:
    # A poster is a second artifact row, and the summary of one row cannot name another.
    # @TODO fill this in when a profile with a POSTER part is built (plan/14-api-schema.md).
    assert render()["poster_url"] is None


def test_the_role_is_serialised_as_text() -> None:
    assert render(role=ArtifactRole.TRANSCRIPT)["role"] == "TRANSCRIPT"


def test_timestamps_are_rfc_3339_with_z() -> None:
    assert render()["created_at"] == "2026-08-05T09:12:03Z"


def test_the_view_is_frozen() -> None:
    view = ArtifactSummaryView.from_record(make_artifact())
    with pytest.raises(ValueError, match="frozen"):
        view.size_bytes = 0
