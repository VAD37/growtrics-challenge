"""`requests` and `jobs`, in memory, plus the transaction that writes a submission.

Three doubles that share one `MemoryDatabase`, matching the three ports orchestration holds over
this data: read the body as it arrived, read and move the job, and write the whole submission at
once.

The two behaviours worth reading closely are both here.

`MemoryUnitOfWork.commit_submission` is a real transaction, not a loop with a comment. It builds
copies of the three tables, writes into the copies, and swaps them in as the last statement. A
failure anywhere before that swap -- a duplicate key, or the injected fault a test uses -- leaves
the database exactly as it was, which is the property `Submission` exists to guarantee.

`MemoryJobRepository.apply_transition` matches on `(job_id, version)` and refuses a mismatch.
`None` on `brief_id`, `artifact_id` or `failure` means "leave the column alone" and never
"clear it" (`orchestration/ports.py`), so a status change on its way to `SUCCEEDED` cannot drop
the brief id the intake stage already recorded.
"""

from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.domain.access import AccessScope
from app.domain.enums import JobStatus
from app.domain.ids import JobId, PrincipalId, RequestKey, WorkItemId
from app.domain.records import Cursor, JobRecord, Page, StoredRequest
from app.orchestration.ports import Clock, JobTransition, QueuedWorkItem, Submission
from app.storage.memory.state import (
    IntegrityError,
    MemoryDatabase,
    RowNotFoundError,
    VersionConflictError,
    WorkItemRow,
    keyset_page,
)

__all__ = ["CommitPoint", "MemoryJobRepository", "MemoryRequestStore", "MemoryUnitOfWork"]

ACTIVE_STATUSES: Final[frozenset[JobStatus]] = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
"""What the `jobs_active` partial index covers, and what admission counts (D090)."""


class CommitPoint(StrEnum):
    """Where a test can make `commit_submission` fail.

    Named after the row that has just been written, so `CommitPoint.JOB` means "two of the three
    rows are staged". Injecting the failure at the last point is the one that matters: it proves
    the swap is what commits, rather than the writes.
    """

    REQUEST = "REQUEST"
    JOB = "JOB"
    WORK_ITEM = "WORK_ITEM"


class InjectedCommitFailure(RuntimeError):
    """What a test's `fail_after` raises. Never raised by a code path of its own."""


class MemoryRequestStore:
    """`RequestStore`: the body exactly as it arrived, keyed by `request_key`.

    Read-only by design. The row is written inside `MemoryUnitOfWork` and never updated (scope
    override item 4), so there is no write method here to update it with.
    """

    def __init__(self, database: MemoryDatabase) -> None:
        self._database: MemoryDatabase = database

    async def load(self, request_key: RequestKey) -> StoredRequest | None:
        return self._database.requests.get(request_key)


class MemoryJobRepository:
    """`JobRepository`: the `jobs` table. Reads take a scope; one method writes."""

    def __init__(self, database: MemoryDatabase, clock: Clock) -> None:
        self._database: MemoryDatabase = database
        self._clock: Clock = clock

    async def count_active(self, scope: AccessScope, principal_id: PrincipalId) -> int:
        """`QUEUED` or `RUNNING` jobs for one principal (D090).

        `principal_id` is the key of the count and the scope is not consulted, deliberately.
        This is a capacity gate: counting whatever the caller may read rather than what they
        own would let one busy principal spend everybody else's allowance.
        """
        del scope
        return sum(
            1
            for job in self._database.jobs.values()
            if job.principal_id == principal_id and job.status in ACTIVE_STATUSES
        )

    async def get(self, scope: AccessScope, job_id: JobId) -> JobRecord | None:
        del scope  # @audit no ownership predicate applied (scope override item 1)
        return self._database.jobs.get(job_id)

    async def list(
        self, scope: AccessScope, *, cursor: Cursor | None, limit: int
    ) -> Page[JobRecord]:
        """Newest first, keyset paged over `(created_at, job_id)`."""
        del scope  # @audit no ownership predicate applied: every job, whoever owns it
        return keyset_page(
            self._database.jobs.values(),
            sort_key=lambda job: (job.created_at, job.job_id),
            cursor=cursor,
            limit=limit,
        )

    async def load_for_run(self, job_id: JobId) -> JobRecord | None:
        """No scope: the worker reading its own row, on a path no request reaches."""
        return self._database.jobs.get(job_id)

    async def list_untouched_since(
        self, status: JobStatus, moment: datetime, *, limit: int
    ) -> tuple[JobRecord, ...]:
        """Jobs still in `status` whose row has not been written since `moment`, oldest first.

        The predicate is `updated_at`, never `created_at`. A job submitted last week that moved a
        stage a second ago has not stopped, and a sweep that read the submit time would fail it
        for being old. `updated_at` is what `apply_transition` stamps, so "untouched" here means
        exactly "nothing has moved this row", which is the question the sweep is asking.

        Inclusive at `moment`, matching `policy.lease_expired`: at the edge the wait is over.

        Capped and oldest first because the backlog this exists for is tens of thousands deep. A
        tick that drains the worst of it beats one that reads all of it and then times out.

        No scope, for `load_for_run`'s reason: this is the system reading its own backlog, and a
        scope here would imply a caller who could ask for somebody else's.
        """
        waiting = sorted(
            (
                job
                for job in self._database.jobs.values()
                if job.status is status and job.updated_at <= moment
            ),
            key=lambda job: (job.updated_at, job.job_id),
        )
        return tuple(waiting[:limit])

    async def apply_transition(self, transition: JobTransition) -> JobRecord:
        """The only write to `jobs` (D066). Returns the row as it now stands.

        Raises `VersionConflictError` when `expected_version` has moved: a lost update is a test
        failure here rather than a status that silently disagrees with the run that produced it.
        """
        current = self._database.jobs.get(transition.job_id)
        if current is None:
            raise RowNotFoundError(f"no job {transition.job_id}")
        if current.version != transition.expected_version:
            raise VersionConflictError(
                f"job {transition.job_id} is at version {current.version}, "
                f"transition expected {transition.expected_version}"
            )
        updated = replace(
            current,
            status=transition.status,
            stage=transition.stage,
            progress_percent=transition.progress_percent,
            brief_id=current.brief_id if transition.brief_id is None else transition.brief_id,
            artifact_id=(
                current.artifact_id if transition.artifact_id is None else transition.artifact_id
            ),
            failure=current.failure if transition.failure is None else transition.failure,
            version=current.version + 1,
            updated_at=self._clock.now(),
        )
        self._database.jobs[updated.job_id] = updated
        return updated


class MemoryUnitOfWork:
    """`UnitOfWork`: the three rows one `POST /v1/jobs` writes, or none of them.

    `fail_after` is the fault injection point. It exists because "these three land together" is
    only worth claiming if something has tried to break it, and a test cannot make a dictionary
    run out of disk.
    """

    def __init__(self, database: MemoryDatabase, *, fail_after: CommitPoint | None = None) -> None:
        self._database: MemoryDatabase = database
        self._fail_after: CommitPoint | None = fail_after
        self.commits: int = 0
        """How many submissions were attempted, successful or not."""

    async def commit_submission(self, submission: Submission) -> None:
        """Insert the request, the job and the work item in one transaction."""
        self.commits += 1
        requests = dict(self._database.requests)
        jobs = dict(self._database.jobs)
        work_items = dict(self._database.work_items)

        self._insert_request(requests, submission.request)
        self._fail_at(CommitPoint.REQUEST)
        self._insert_job(jobs, submission.job)
        self._fail_at(CommitPoint.JOB)
        self._insert_work_item(work_items, submission.work_item)
        self._fail_at(CommitPoint.WORK_ITEM)

        # The commit. Everything above touched copies, so any refusal on the way here left the
        # database untouched rather than half written.
        self._database.requests = requests
        self._database.jobs = jobs
        self._database.work_items = work_items

    def _fail_at(self, point: CommitPoint) -> None:
        if self._fail_after is point:
            raise InjectedCommitFailure(f"injected failure after the {point.value} row")

    @staticmethod
    def _insert_request(rows: dict[RequestKey, StoredRequest], request: StoredRequest) -> None:
        if request.request_key in rows:
            raise IntegrityError(f"request {request.request_key} already exists")
        rows[request.request_key] = request

    @staticmethod
    def _insert_job(rows: dict[JobId, JobRecord], job: JobRecord) -> None:
        if job.job_id in rows:
            raise IntegrityError(f"job {job.job_id} already exists")
        rows[job.job_id] = job

    @staticmethod
    def _insert_work_item(rows: dict[WorkItemId, WorkItemRow], item: QueuedWorkItem) -> None:
        """One queue row per job: `derive_work_item_id` collides rather than queueing twice."""
        if item.item_id in rows:
            raise IntegrityError(f"work item {item.item_id} already exists")
        rows[item.item_id] = WorkItemRow.queued(item)
