"""Every port over Postgres except the queue, which is `queue.py`.

Six adapters, one per table the demo writes, holding between them the same behaviour
`app/storage/memory/` holds over five dictionaries. The contract suite in `tests/contract/` runs
one set of assertions against both, which is the only thing that makes heavy use of doubles safe:
a double that has drifted is a green suite about a system nobody is running.

Four things here are decisions rather than translation.

**The transaction boundary is one `async with`.** `SqlEngine.connection()` commits on a clean
exit and rolls back on an exception, so `commit_submission` writes three rows in one transaction
by doing nothing special, and a method that raises halfway leaves the database as it was. Each
method opens its own connection, which is also why no two of them can end up sharing one.

**Reads take an `AccessScope` and do not consult it.** @audit that is the demo's whole
authorisation model (`app/domain/access.py`, scope override item 1). The predicate-free statement
is what runs; restoring ownership means adding `AND principal_id = %(principal_id)s` to three
statements in this file and minting the scope with `ownership_enforced=True`. The argument is
already in every signature (D067), so that change fills in bodies rather than re-signing methods.

**Paging is keyset and never offset** (D072). `(created_at, id) < (cursor)` under
`ORDER BY created_at DESC, id DESC` is exactly `keyset_page`'s comparison, and one extra row is
fetched so "is there another page" costs no second query. An offset would skip and repeat rows
under the concurrent inserts a job list gets constantly.

**Time comes from the `Clock` port, never from `now()`.** Every timestamp this file writes is a
parameter. The defaults in `schema.sql` are there for a human typing an INSERT, and a repository
that let them fire would put the database's clock on some rows and the application's on others,
which is the kind of disagreement that only surfaces in a test that freezes time.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Final

import psycopg

from app.domain.access import AccessScope, Principal
from app.domain.enums import JobStatus
from app.domain.ids import ArtifactId, BriefId, JobId, PrincipalId, RequestKey
from app.domain.records import (
    ArtifactRecord,
    BriefRecord,
    Cursor,
    JobRecord,
    Page,
    StoredRequest,
)
from app.orchestration.ports import Clock, JobTransition, Submission
from app.storage.errors import IntegrityError, RowNotFoundError, VersionConflictError
from app.storage.sql.engine import SqlEngine
from app.storage.sql.rows import (
    ARTIFACT_COLUMNS,
    BRIEF_COLUMNS,
    JOB_COLUMNS,
    PRINCIPAL_COLUMNS,
    REQUEST_COLUMNS,
    Params,
    artifact_from_row,
    artifact_params,
    brief_from_row,
    brief_params,
    failure_json,
    job_from_row,
    job_params,
    principal_from_row,
    request_from_row,
    request_params,
)

__all__ = [
    "SqlArtifactRepository",
    "SqlBriefRepository",
    "SqlJobRepository",
    "SqlPrincipalRepository",
    "SqlRequestStore",
    "SqlUnitOfWork",
]

ACTIVE_STATUSES: Final[list[str]] = [JobStatus.QUEUED.value, JobStatus.RUNNING.value]
"""What `jobs_active` is partial on, and what admission counts (D090).

Passed as a parameter rather than written into the statement, so the count and the index
predicate in `schema.sql` cannot drift apart without this line changing too.
"""

type PageKey[R] = Callable[[R], tuple[datetime, str]]


def _page[R](rows: list[R], *, limit: int, key: PageKey[R]) -> Page[R]:
    """One page out of `limit + 1` rows, and a cursor only if the extra row came back.

    Matches `storage/memory/state.py::keyset_page` including both of its edges: an empty window
    has no cursor, and a window that swallowed everything left has none either. A cursor on the
    last page costs a client one more round trip to be told there is nothing behind it.
    """
    window = rows[:limit]
    if not window or len(rows) == len(window):
        return Page(items=tuple(window), next_cursor=None)
    created_at, row_id = key(window[-1])
    return Page(items=tuple(window), next_cursor=Cursor(created_at=created_at, id=row_id).encode())


def _cursor_params(cursor: Cursor | None) -> Params:
    """A cursor as two nullable parameters, so one statement serves the first page and the rest.

    Two statements differing by a `WHERE` clause would be two statements to keep in step, and the
    one nobody exercises is the one that rots. `NULL` in both means "start at the newest".
    """
    return {
        "cursor_created_at": None if cursor is None else cursor.created_at,
        "cursor_id": None if cursor is None else cursor.id,
    }


def _transition_params(transition: JobTransition, now: datetime) -> Params:
    return {
        "job_id": transition.job_id,
        "expected_version": transition.expected_version,
        "status": transition.status.value,
        "stage": transition.stage.value,
        "progress_percent": transition.progress_percent,
        "brief_id": transition.brief_id,
        "artifact_id": transition.artifact_id,
        "failure": failure_json(transition.failure),
        "now": now,
    }


class SqlPrincipalRepository:
    """`access.ports.PrincipalRepository`: the row every other table has a foreign key to."""

    def __init__(self, engine: SqlEngine, clock: Clock) -> None:
        self._engine: Final[SqlEngine] = engine
        self._clock: Final[Clock] = clock

    async def upsert(self, *, principal_id: PrincipalId, external_id: str) -> Principal:
        """Return the existing row, or admit this caller.

        `DO UPDATE` writing a column back to itself, rather than `DO NOTHING`. The two agree on
        the result and disagree under a race, which is the case this has to survive: two first
        requests from one caller arrive together, and `DO NOTHING` returns no row to the loser
        while the winner's insert is still uncommitted. `DO UPDATE` waits for that commit and
        then returns the row, so this port never has to answer `None` to "admit this caller".

        The conflict branch changes nothing. `created_at` is the first sighting and a later
        request must not move it, and `external_id` is the identity the row was created under,
        which a read path has no business rewriting.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"INSERT INTO principals ({PRINCIPAL_COLUMNS}) "
                "VALUES (%(principal_id)s, %(external_id)s, %(created_at)s) "
                "ON CONFLICT (principal_id) DO UPDATE SET external_id = principals.external_id "
                f"RETURNING {PRINCIPAL_COLUMNS}",
                {
                    "principal_id": principal_id,
                    "external_id": external_id,
                    "created_at": self._clock.now(),
                },
            )
            row = await cursor.fetchone()
        if row is None:
            raise RowNotFoundError(f"upsert of principal {principal_id} returned no row")
        return principal_from_row(row)


class SqlRequestStore:
    """`orchestration.ports.RequestStore`: the body as it arrived, read side only.

    The row is written inside `SqlUnitOfWork.commit_submission` and never updated (scope override
    item 4), so there is deliberately no write method here to update it with.
    """

    def __init__(self, engine: SqlEngine) -> None:
        self._engine: Final[SqlEngine] = engine

    async def load(self, request_key: RequestKey) -> StoredRequest | None:
        """No scope: the runner reads this, not a caller."""
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {REQUEST_COLUMNS} FROM requests WHERE request_key = %(request_key)s",
                {"request_key": request_key},
            )
            row = await cursor.fetchone()
        return None if row is None else request_from_row(row)


class SqlJobRepository:
    """`orchestration.ports.JobRepository`: the `jobs` table. Reads take a scope; one writes."""

    def __init__(self, engine: SqlEngine, clock: Clock) -> None:
        self._engine: Final[SqlEngine] = engine
        self._clock: Final[Clock] = clock

    async def count_active(self, scope: AccessScope, principal_id: PrincipalId) -> int:
        """`QUEUED` or `RUNNING` jobs for one principal (D090).

        `principal_id` is the key of the count and the scope is not consulted, deliberately.
        This is a capacity gate: counting whatever the caller may read rather than what they own
        would let one busy principal spend everybody else's allowance.
        """
        del scope
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) FROM jobs "
                "WHERE principal_id = %(principal_id)s AND status = ANY(%(statuses)s)",
                {"principal_id": principal_id, "statuses": ACTIVE_STATUSES},
            )
            row = await cursor.fetchone()
        return 0 if row is None else int(row[0])

    async def get(self, scope: AccessScope, job_id: JobId) -> JobRecord | None:
        """@audit no ownership predicate applied: the scope is carried and not consulted."""
        del scope
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = %(job_id)s",
                {"job_id": job_id},
            )
            row = await cursor.fetchone()
        return None if row is None else job_from_row(row)

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        """Newest first, keyset paged over `(created_at, job_id)`.

        @audit no ownership predicate applied: every job in the database, whoever owns it.

        @TODO that audit line is also why there is no index behind this. `jobs_by_principal` is
        `(principal_id, created_at DESC)` and cannot serve an unfiltered sort of the whole table,
        so this plans as a sort until the ownership predicate comes back -- at which point the
        index serves it exactly and nothing here changes. Adding a second index for the unscoped
        version would be paying, on every insert, to keep a query fast that is not supposed to
        exist.
        """
        del scope
        async with self._engine.connection() as connection:
            result = await connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs "
                "WHERE %(cursor_created_at)s::timestamptz IS NULL "
                "   OR (created_at, job_id) "
                "     < (%(cursor_created_at)s::timestamptz, %(cursor_id)s::text) "
                "ORDER BY created_at DESC, job_id DESC "
                "LIMIT %(limit)s",
                {**_cursor_params(cursor), "limit": limit + 1},
            )
            rows = await result.fetchall()
        return _page(
            [job_from_row(row) for row in rows],
            limit=limit,
            key=lambda job: (job.created_at, job.job_id),
        )

    async def load_for_run(self, job_id: JobId) -> JobRecord | None:
        """The row a claimed work item points at.

        No scope, because there is no caller: this is the system reading its own row on the way
        to executing it. Its own statement rather than a call to `get`, so the two read paths
        stay separately greppable on the day one of them grows an ownership predicate and the
        other must not.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = %(job_id)s",
                {"job_id": job_id},
            )
            row = await cursor.fetchone()
        return None if row is None else job_from_row(row)

    async def list_untouched_since(
        self, status: JobStatus, moment: datetime, *, limit: int
    ) -> tuple[JobRecord, ...]:
        """Jobs still in `status` whose row has not been written since `moment`, oldest first.

        The predicate is `updated_at` and never `created_at`: a job submitted last week that
        moved a stage a second ago has not stopped, and a sweep reading the submit time would
        fail it for being old.

        Inclusive at `moment`, matching `policy.lease_expired`. Capped and oldest first because
        the backlog this exists for is tens of thousands deep, and `jobs_active` carries
        `updated_at` as its second column so the ordering is a range scan rather than a sort of
        every queued row.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs "
                "WHERE status = %(status)s AND updated_at <= %(moment)s "
                "ORDER BY updated_at, job_id "
                "LIMIT %(limit)s",
                {"status": status.value, "moment": moment, "limit": limit},
            )
            rows = await cursor.fetchall()
        return tuple(job_from_row(row) for row in rows)

    async def apply_transition(self, transition: JobTransition) -> JobRecord:
        """The only write to `jobs` in the system (D066). Returns the row as it now stands.

        `COALESCE(%(column)s, column)` is "leave it alone", which is what `None` means on
        `brief_id`, `artifact_id` and `failure` (`orchestration/ports.py`). Writing the parameter
        straight in would clear the brief id on the way to `SUCCEEDED`, which is the easiest bug
        in this file to write and the hardest to see afterwards. The casts are not decoration:
        an untyped `NULL` beside `COALESCE` leaves the parameter's type undecidable.

        Matching on `version` is the optimistic concurrency check. Zero rows updated is either a
        stale version or no row at all, and those are different failures to a caller, so the
        second statement asks which -- inside the same transaction, so nothing moves between the
        two questions.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                "UPDATE jobs SET "
                "    status = %(status)s, "
                "    stage = %(stage)s, "
                "    progress_percent = %(progress_percent)s, "
                "    brief_id = COALESCE(%(brief_id)s::text, brief_id), "
                "    artifact_id = COALESCE(%(artifact_id)s::text, artifact_id), "
                "    failure = COALESCE(%(failure)s::jsonb, failure), "
                "    version = version + 1, "
                "    updated_at = %(now)s "
                "WHERE job_id = %(job_id)s AND version = %(expected_version)s "
                f"RETURNING {JOB_COLUMNS}",
                _transition_params(transition, self._clock.now()),
            )
            row = await cursor.fetchone()
            if row is not None:
                return job_from_row(row)
            current = await connection.execute(
                "SELECT version FROM jobs WHERE job_id = %(job_id)s",
                {"job_id": transition.job_id},
            )
            found = await current.fetchone()
        if found is None:
            raise RowNotFoundError(f"no job {transition.job_id}")
        raise VersionConflictError(
            f"job {transition.job_id} is at version {found[0]}, "
            f"transition expected {transition.expected_version}"
        )


class SqlUnitOfWork:
    """`orchestration.ports.UnitOfWork`: the three rows one `POST /v1/jobs` writes, or none.

    The atomicity is Postgres's, not this class's. What this class owns is that nothing between
    the first insert and the last can commit on its own: one connection, one implicit
    transaction, and a rollback on the way out of the `async with` if anything raises.
    """

    def __init__(self, engine: SqlEngine) -> None:
        self._engine: Final[SqlEngine] = engine

    async def commit_submission(self, submission: Submission) -> None:
        """Insert the request, the job and the work item in one transaction.

        A duplicate key becomes `IntegrityError`, the same class the doubles raise, so an
        at-least-once submit is a refusal orchestration can catch rather than a driver exception
        carrying a table name across a port. A missing `principals` row lands here too, and gets
        the same answer: a row that cannot be written because of a row that is not there.
        """
        item = submission.work_item
        try:
            async with self._engine.connection() as connection:
                await connection.execute(
                    f"INSERT INTO requests ({REQUEST_COLUMNS}) "
                    "VALUES (%(request_key)s, %(principal_id)s, %(raw)s, %(received_at)s)",
                    request_params(submission.request),
                )
                await connection.execute(
                    "INSERT INTO jobs ("
                    "    job_id, request_key, principal_id, chat_context_id, brief_id, status,"
                    "    stage, attempt, progress_percent, profile, output_contract, constraints,"
                    "    artifact_id, budget, failure, version, created_at, updated_at"
                    ") VALUES ("
                    "    %(job_id)s, %(request_key)s, %(principal_id)s, %(chat_context_id)s,"
                    "    %(brief_id)s, %(status)s, %(stage)s, %(attempt)s, %(progress_percent)s,"
                    "    %(profile)s, %(output_contract)s, %(constraints)s, %(artifact_id)s,"
                    "    %(budget)s, %(failure)s, %(version)s, %(created_at)s, %(updated_at)s"
                    ")",
                    job_params(submission.job),
                )
                await connection.execute(
                    "INSERT INTO work_items (item_id, job_id, available_at, created_at) "
                    "VALUES (%(item_id)s, %(job_id)s, %(available_at)s, %(created_at)s)",
                    {
                        "item_id": item.item_id,
                        "job_id": item.job_id,
                        "available_at": item.available_at,
                        "created_at": item.created_at,
                    },
                )
        except psycopg.IntegrityError as error:
            raise IntegrityError(f"submission {submission.job.job_id} refused: {error}") from error


class SqlBriefRepository:
    """`intake.ports.BriefRepository`: the `briefs` table. Sole writer `intake` (D066)."""

    def __init__(self, engine: SqlEngine) -> None:
        self._engine: Final[SqlEngine] = engine

    async def insert(self, record: BriefRecord) -> BriefRecord:
        """Write a sealed brief. A second one under the same id is a bug, not an update.

        No `ON CONFLICT`. `brief_id` is `uuid5(NS_BRIEF, job_id)`, so a collision means one job
        was sealed twice, and taking the second write would drop the `brief_hash` the first run
        was verified against.
        """
        try:
            async with self._engine.connection() as connection:
                await connection.execute(
                    f"INSERT INTO briefs ({BRIEF_COLUMNS}) VALUES ("
                    "    %(brief_id)s, %(job_id)s, %(brief_hash)s, %(template_version)s,"
                    "    %(subject)s, %(concept_id)s, %(instruction)s, %(context_items)s,"
                    "    %(constraints)s, %(guard_verdict)s, %(sealed_at)s"
                    ")",
                    brief_params(record),
                )
        except psycopg.IntegrityError as error:
            raise IntegrityError(f"brief {record.brief_id} refused: {error}") from error
        return record

    async def load(self, brief_id: BriefId) -> BriefRecord | None:
        """The sealed brief a run works from. No scope: the runner reads it, not a caller."""
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {BRIEF_COLUMNS} FROM briefs WHERE brief_id = %(brief_id)s",
                {"brief_id": brief_id},
            )
            row = await cursor.fetchone()
        return None if row is None else brief_from_row(row)


class SqlArtifactRepository:
    """`custody.ports.ArtifactRepository`: the write half and the two reads that serve it.

    `list` carries the two predicates `artifacts_by_principal` is partial on (A6, D088). They are
    in the statement rather than in a filter over the result because the index is where that
    access rule is enforced: a listing that plans on it cannot reach a quarantined row or an
    operator-audience one, whatever the caller asked for.
    """

    def __init__(self, engine: SqlEngine) -> None:
        self._engine: Final[SqlEngine] = engine

    async def insert(self, record: ArtifactRecord) -> ArtifactRecord:
        """Write the row custody has already stored the bytes for.

        A second insert under the same id is refused rather than merged: `artifact_id` is
        `uuid5(job_id | content_hash)`, so a collision means the same job produced the same bytes
        twice, and taking the second write would overwrite the verdict recorded against the
        first.
        """
        try:
            async with self._engine.connection() as connection:
                await connection.execute(
                    f"INSERT INTO artifacts ({ARTIFACT_COLUMNS}) VALUES ("
                    "    %(artifact_id)s, %(job_id)s, %(principal_id)s, %(chat_context_id)s,"
                    "    %(role)s, %(audience)s, %(mime)s, %(rel_path)s, %(size_bytes)s,"
                    "    %(content_hash)s, %(storage_uri)s, %(probe)s, %(scan_verdict)s,"
                    "    %(validator_version)s, %(published_at)s, %(created_at)s"
                    ")",
                    artifact_params(record),
                )
        except psycopg.IntegrityError as error:
            raise IntegrityError(f"artifact {record.artifact_id} refused: {error}") from error
        return record

    async def get(self, scope: AccessScope, artifact_id: ArtifactId) -> ArtifactRecord | None:
        """The row behind a content request, verdict and audience included.

        Unfiltered on purpose: `custody.store.content_stream` is what refuses a quarantined or
        operator-audience row, and it needs to see one to refuse it.

        @audit no ownership predicate applied: any caller reads any artifact row.
        """
        del scope
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE artifact_id = %(artifact_id)s",
                {"artifact_id": artifact_id},
            )
            row = await cursor.fetchone()
        return None if row is None else artifact_from_row(row)

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        """`LEARNER` and `CLEAN` only, newest first, optionally one job's output.

        @audit no ownership predicate applied: every learner artifact in the database, whoever
        made it (scope override item 1).
        """
        del scope
        async with self._engine.connection() as connection:
            result = await connection.execute(
                f"SELECT {ARTIFACT_COLUMNS} FROM artifacts "
                "WHERE audience = 'LEARNER' AND scan_verdict = 'CLEAN' "
                "  AND (%(job_id)s::text IS NULL OR job_id = %(job_id)s::text) "
                "  AND (%(cursor_created_at)s::timestamptz IS NULL "
                "       OR (created_at, artifact_id) "
                "         < (%(cursor_created_at)s::timestamptz, %(cursor_id)s::text)) "
                "ORDER BY created_at DESC, artifact_id DESC "
                "LIMIT %(limit)s",
                {**_cursor_params(cursor), "job_id": job_id, "limit": limit + 1},
            )
            rows = await result.fetchall()
        return _page(
            [artifact_from_row(row) for row in rows],
            limit=limit,
            key=lambda record: (record.created_at, record.artifact_id),
        )
