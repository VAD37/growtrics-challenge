"""The artifact card: enough to render it, and nothing custody keeps to itself.

One shape serves two places, `JobView.artifact` and the items of `GET /v1/artifacts`, so
whatever it leaks it leaks twice. `storage_uri`, `content_hash`, `probe`, `scan_verdict`,
`validator_version`, and `principal_id` are on the row and stay behind the API: the first is an
internal address, the middle three are evidence for an operator, and the last is another
learner's identity.

`ArtifactView`, `DeliverableView` and `VerificationView` from `plan/14-api-schema.md` are not
built. The demo has no deliverable endpoint (D091) and they return additively with it.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from app.api.schemas.common import NestedView, PageView
from app.domain.enums import ArtifactRole
from app.domain.ids import ArtifactId
from app.domain.records import ArtifactRecord

CONTENT_PATH_TEMPLATE: Final[str] = "/v1/artifacts/{artifact_id}/content"

ARTIFACT_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "artifact_id",
    "role",
    "media_type",
    "size_bytes",
    "duration_s",
    "poster_url",
    "content_url",
    "created_at",
)
"""The serialised key set, written down so a test can assert it rather than infer it."""

_DURATION_PROBE_KEY: Final[str] = "duration_s"


def content_url_for(artifact_id: ArtifactId) -> str:
    return CONTENT_PATH_TEMPLATE.format(artifact_id=artifact_id)


def duration_from_probe(probe: Mapping[str, object]) -> float | None:
    """Read the duration out of the probe blob, or admit there is not one.

    `probe` is jsonb written by custody and its keys differ per media type and per prober, so
    the edge reads it defensively. A missing or non-numeric value is `null`, never a zero: a
    zero-second video is a claim, and nothing here measured one. `bool` is excluded because it
    is an `int` in Python and `true` is not a duration.
    """
    value = probe.get(_DURATION_PROBE_KEY)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class ArtifactSummaryView(NestedView):
    """Embedded in `JobView` and in list responses (`plan/14-api-schema.md`)."""

    artifact_id: ArtifactId
    role: ArtifactRole
    media_type: str
    size_bytes: int
    duration_s: float | None
    poster_url: str | None
    content_url: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: ArtifactRecord) -> ArtifactSummaryView:
        """`media_type` is the `mime` column: our probe's answer, never the worker's manifest.

        `poster_url` is `None` in the demo. A poster is a second artifact row and the summary
        of one row cannot name another. @TODO fill it in with the profile that has a POSTER
        part, which needs the deliverable read the demo cut (`plan/14-api-schema.md`, D091).
        """
        return cls(
            artifact_id=record.artifact_id,
            role=record.role,
            media_type=record.mime,
            size_bytes=record.size_bytes,
            duration_s=duration_from_probe(record.probe),
            poster_url=None,
            content_url=content_url_for(record.artifact_id),
            created_at=record.created_at,
        )


class ArtifactPage(PageView[ArtifactSummaryView]):
    """`GET /v1/artifacts`, with or without `?job_id=`. Newest first, cursor paged."""
