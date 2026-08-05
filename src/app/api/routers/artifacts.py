"""`GET /v1/artifacts` and `GET /v1/artifacts/{artifact_id}/content`.

Steps 4 and 5 of the success test in `docs/demo.md`. One listing endpoint answers both
"everything I have made" and "what did this job produce", which is why step 4 costs one
endpoint rather than two, and one content endpoint is the only path from a stored object to a
client.

`/v1/contexts/{ctx}/artifacts` keeps its frozen meaning and is not built: the demo has no chat
contexts (D087).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Path, Query
from fastapi.responses import StreamingResponse

from app.api.deps import ArtifactServiceDep, PageDep, ScopeDep
from app.api.errors import ERROR_RESPONSES
from app.api.schemas.artifacts import ArtifactPage, ArtifactSummaryView
from app.domain.ids import ARTIFACT_ID_PATTERN, JOB_ID_PATTERN

router: Final[APIRouter] = APIRouter(prefix="/v1", tags=["artifacts"], responses=ERROR_RESPONSES)

ArtifactIdPath = Annotated[str, Path(pattern=ARTIFACT_ID_PATTERN)]
JobIdQuery = Annotated[str | None, Query(pattern=JOB_ID_PATTERN)]

_FILENAME_SAFE: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_FILENAME_MAX_CHARS: Final[int] = 96
_FILENAME_FALLBACK: Final[str] = "artifact"


def safe_filename(filename: str) -> str:
    """Reduce a filename to characters that cannot end a header early or start a new one.

    @audit the name on an artifact row came from a worker's manifest by way of custody, which
    makes it untrusted text that this module writes into a response header. Starlette would
    reject a raw newline, but the quoting rules for `Content-Disposition` are subtle enough
    that an allowlist is the cheaper argument: what survives is `[A-Za-z0-9._-]`, so a quote,
    a semicolon, a CR or an LF cannot reach the wire at all.
    """
    kept = "".join(char for char in filename if char in _FILENAME_SAFE)[:_FILENAME_MAX_CHARS]
    return kept.lstrip(".") or _FILENAME_FALLBACK


@router.get("/artifacts", response_model=ArtifactPage)
async def list_artifacts(
    scope: ScopeDep,
    artifacts: ArtifactServiceDep,
    page: PageDep,
    job_id: JobIdQuery = None,
) -> ArtifactPage:
    """Newest first, cursor paged, optional `?job_id=`.

    Quarantined rows and operator-audience rows never appear, and that exclusion lives in the
    partial index behind the seam rather than in a filter this router could forget (D073, A6).
    """
    found = await artifacts.list(scope, job_id=job_id, cursor=page.cursor, limit=page.limit)
    return ArtifactPage(
        items=[ArtifactSummaryView.from_record(record) for record in found.items],
        next_cursor=found.next_cursor,
    )


@router.get("/artifacts/{artifact_id}/content", response_class=StreamingResponse)
async def get_artifact_content(
    artifact_id: ArtifactIdPath,
    scope: ScopeDep,
    artifacts: ArtifactServiceDep,
) -> StreamingResponse:
    """The bytes, streamed.

    Never buffered: a 64 MiB video held in memory per concurrent download is the whole reason
    `ContentStream.chunks` is an async iterator. The headers are custody's decisions rendered,
    not this router's: `media_type` is our probe's answer rather than a worker's claim,
    `disposition` comes from the profile's serving policy, and `nosniff` is on unconditionally
    so a browser cannot decide the type for itself.

    `404` when there is no such artifact and `409 ARTIFACT_NOT_READY` when the row exists with
    no bytes; both are raised behind the seam and mapped by `app/api/errors.py`.
    """
    stream = await artifacts.open_content(scope, artifact_id)
    disposition = f'{stream.disposition}; filename="{safe_filename(stream.filename)}"'
    return StreamingResponse(
        stream.chunks,
        media_type=stream.media_type,
        headers={
            "Content-Length": str(stream.size_bytes),
            "ETag": f'"{stream.content_hash}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": disposition,
        },
    )
