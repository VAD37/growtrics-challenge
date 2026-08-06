"""The `artifacts` table in memory: one writer, two reads.

`insert` is `custody.ports.ArtifactWriter`, the only way a row gets here (D066). With the two
reads it is `custody.ports.ArtifactRepository`. Those reads
are the ones `GET /v1/artifacts` and `GET /v1/artifacts/{id}/content` need, and both take an
`AccessScope` first (D067).

`list` applies by hand what `artifacts_by_principal` applies as a partial index (A6, D088): only
a `CLEAN`, `LEARNER` row is listable. Doing it in the query rather than in a filter the edge
could forget is the point of that index, and a double that returned quarantined rows would make
the edge look correct while the index was doing the work.
"""

from app.domain.access import AccessScope
from app.domain.enums import Audience, ScanVerdict
from app.domain.ids import ArtifactId, JobId
from app.domain.records import ArtifactRecord, Cursor, Page
from app.storage.memory.state import IntegrityError, MemoryDatabase, keyset_page

__all__ = ["MemoryArtifactRepository"]


class MemoryArtifactRepository:
    """`custody.ports.ArtifactRepository`: the write half and the two reads that serve it."""

    def __init__(self, database: MemoryDatabase) -> None:
        self._database: MemoryDatabase = database

    async def insert(self, record: ArtifactRecord) -> ArtifactRecord:
        """Write the row custody has already stored the bytes for.

        A second insert under the same id is refused rather than merged. `artifact_id` is
        `uuid5(job_id | content_hash)`, so a collision means the same job produced the same bytes
        twice, and taking the second write would overwrite the verdict recorded against the
        first.
        """
        if record.artifact_id in self._database.artifacts:
            raise IntegrityError(f"artifact {record.artifact_id} already exists")
        self._database.artifacts[record.artifact_id] = record
        return record

    async def get(self, scope: AccessScope, artifact_id: ArtifactId) -> ArtifactRecord | None:
        """The row behind a content request, verdict and audience included.

        Unfiltered on purpose: `custody.store.content_stream` is what refuses a quarantined or
        operator-audience row, and it needs to see one to refuse it.

        @audit no ownership predicate applied: any caller reads any artifact row.
        """
        del scope
        return self._database.artifacts.get(artifact_id)

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        """`LEARNER` and `CLEAN` only, newest first, optionally one job's output.

        @audit no ownership predicate applied: this lists every learner artifact in the
        database, whoever made it (scope override item 1).
        """
        del scope
        listable = [
            row
            for row in self._database.artifacts.values()
            if row.audience is Audience.LEARNER
            and row.scan_verdict is ScanVerdict.CLEAN
            and (job_id is None or row.job_id == job_id)
        ]
        return keyset_page(
            listable,
            sort_key=lambda row: (row.created_at, row.artifact_id),
            cursor=cursor,
            limit=limit,
        )
