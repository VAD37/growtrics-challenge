"""Getting a backend into a state, through its ports and not around them.

Three helpers, and between them they are how every contract test arranges its rows. They go
through the ports on purpose: a suite that reached past them to write rows directly would be
asserting about a database it had populated with a second, untested writer, and the two would
agree right up until one of them was wrong.

`hold` on `StorageBackend` is the one exception, and it is documented where it lives.
"""

from datetime import datetime

from app.domain.enums import JobStatus, StageName, percent_for
from app.domain.ids import PrincipalId
from app.domain.records import JobRecord
from app.orchestration.ports import JobTransition
from support.backends import StorageBackend
from support.rows import PRINCIPAL, T0, make_submission


async def admit(backend: StorageBackend, principal_id: PrincipalId = PRINCIPAL) -> None:
    """Upsert the principal row every other table has a foreign key to.

    Idempotent, so a test that submits four jobs for one caller can call it four times without
    caring, which is exactly what `POST /v1/jobs` does on every request.
    """
    await backend.principals.upsert(principal_id=principal_id, external_id=principal_id)


async def submit(
    backend: StorageBackend,
    index: int = 0,
    *,
    principal_id: PrincipalId = PRINCIPAL,
    created_at: datetime = T0,
) -> JobRecord:
    """One submission: the principal, then the request, the job and the queue row together.

    Returns the job as the caller built it, which is also the row now in the database. The work
    item is available at `created_at`, so a test that wants a queue row that is not due yet
    submits it into the future.
    """
    await admit(backend, principal_id)
    submission = make_submission(index, principal_id=principal_id, created_at=created_at)
    await backend.unit_of_work.commit_submission(submission)
    return submission.job


async def touch(
    backend: StorageBackend,
    job: JobRecord,
    *,
    at: datetime,
    status: JobStatus | None = None,
    stage: StageName | None = None,
) -> JobRecord:
    """Move the clock to `at` and write one transition, so `updated_at` becomes `at`.

    The only way to separate `updated_at` from `created_at`, and it is the way the running system
    does it: `apply_transition` is the sole writer of the `jobs` row (D066), and the sweep's whole
    question is which rows it has not touched lately. A test that set the column directly would
    be asserting about a column nothing writes that way.

    `status` and `stage` default to where the job already is, so "this row was written at `at`"
    costs no unrelated state change.
    """
    backend.clock.at = at
    to_status = job.status if status is None else status
    to_stage = job.stage if stage is None else stage
    return await backend.jobs.apply_transition(
        JobTransition(
            job_id=job.job_id,
            expected_version=job.version,
            status=to_status,
            stage=to_stage,
            progress_percent=percent_for(to_stage),
        )
    )
