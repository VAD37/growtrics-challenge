"""The seam between the HTTP edge and the work behind it.

Structural typing, and it lives here rather than in `app/orchestration/` for one reason: the
API must not import orchestration, and orchestration must not import the API. A `Protocol`
declared on the API's side is satisfied by the use-case classes without either package naming
the other, so the import-linter contracts stay true and both sides still agree on one
signature (`plan/03-module-layout.md`, rule 2).

Every method takes an `AccessScope` first (D067). In the demo that scope enforces nothing --
see `app/access/stub.py` -- but the argument is there, so restoring authorisation fills in
bodies rather than re-signing every method.

The API never touches a table, so `submit` carries the raw request body across as well as the
validated command: `requests` holds what the caller typed and `briefs` holds what we asked
for, and only orchestration writes either (scope override item 4, `plan/12-data-control.md`).
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from app.domain.access import AccessScope
from app.domain.ids import ArtifactId, JobId
from app.domain.records import (
    ArtifactRecord,
    ContentStream,
    Cursor,
    JobRecord,
    Page,
    SubmitJobCommand,
)

__all__ = ["ArtifactService", "DatabaseProbe", "JobService"]


@runtime_checkable
class JobService(Protocol):
    """Submit and read jobs. Implemented by `app.orchestration`."""

    async def submit(
        self,
        scope: AccessScope,
        command: SubmitJobCommand,
        raw_body: Mapping[str, object],
    ) -> JobRecord:
        """Mint a request key, store the request, insert the job and its work item.

        Raises `TOO_MANY_ACTIVE_JOBS` when the admission count is already at the ceiling
        (D090). That check is one indexed count in the same transaction as the insert, so it
        belongs behind this seam; the edge only maps the code to `429`.

        There is no replay protection: two identical submits are two jobs (scope override
        item 2, supersedes D055 and D089).
        """
        ...

    async def get(self, scope: AccessScope, job_id: JobId) -> JobRecord:
        """Raises `JOB_NOT_FOUND` when there is no such row.

        @audit and only then. No row is compared against `scope.principal_id`, so any caller
        reads any job.
        """
        ...

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        """Newest first, keyset paged over `(created_at, job_id)` (D072)."""
        ...


@runtime_checkable
class ArtifactService(Protocol):
    """List artifacts and open their bytes. Implemented by `app.custody`."""

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        """One endpoint covers "everything I made" and "what did this job produce".

        Quarantined rows and `OPERATOR` audience rows are excluded behind this seam, by the
        partial index rather than by a filter the edge could forget (D073, A6).
        """
        ...

    async def open_content(self, scope: AccessScope, artifact_id: ArtifactId) -> ContentStream:
        """Raises `ARTIFACT_NOT_FOUND` when there is no row, `ARTIFACT_NOT_READY` when the row
        exists and its bytes do not. Never `404` for the second case: the artifact exists, and
        saying otherwise is a lie the client acts on.
        """
        ...


@runtime_checkable
class DatabaseProbe(Protocol):
    """`GET /health` asks one question and this is it (`docs/demo.md`, "Surface").

    Injected rather than imported so the router is testable without a database and so the
    storage lane owns what "reachable" means.
    """

    async def ping(self) -> bool: ...
