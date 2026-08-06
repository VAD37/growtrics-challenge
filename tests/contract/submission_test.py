"""One submit writes three rows, or it writes none. Against both backends.

`Submission` exists so a half-written submit is not a shape a caller can express
(`orchestration/ports.py`). That is only worth claiming if the transaction behind it is real, so
the test that matters here is the one where the second insert is refused: the first row is
already staged, and afterwards there must be nothing.

The refusal is a duplicate job id under a fresh request key, which is a state both backends
produce for the same reason -- a primary key that already has a row. Injecting the failure any
other way would test one backend's fault injection rather than the property both have to hold.
"""

import pytest
from support.backends import StorageBackend
from support.rows import (
    PRINCIPAL,
    T0,
    demo_scope,
    job_id_for,
    make_job,
    make_request,
    make_submission,
    request_key_for,
)
from support.seed import admit, submit

from app.storage.errors import IntegrityError


async def test_a_submission_writes_the_request_the_job_and_the_work_item(
    backend: StorageBackend,
) -> None:
    await admit(backend)
    submission = make_submission()

    await backend.unit_of_work.commit_submission(submission)

    assert await backend.requests.load(request_key_for(0)) == submission.request
    assert await backend.jobs.load_for_run(job_id_for(0)) == submission.job
    row = await backend.peek(submission.work_item.item_id)
    assert row is not None
    assert row.job_id == job_id_for(0)
    assert row.available_at == T0
    assert row.claimed_by is None
    assert row.claim_count == 0


async def test_the_request_store_reads_what_the_transaction_wrote(
    backend: StorageBackend,
) -> None:
    assert await backend.requests.load(request_key_for(0)) is None

    await submit(backend)

    assert await backend.requests.load(request_key_for(0)) == make_request()


async def test_a_repeated_submission_is_refused_and_changes_nothing(
    backend: StorageBackend,
) -> None:
    """At-least-once delivery collides on the primary key rather than queueing twice."""
    await submit(backend)

    with pytest.raises(IntegrityError):
        await backend.unit_of_work.commit_submission(make_submission())

    page = await backend.jobs.list(demo_scope(), cursor=None, limit=10)
    assert len(page.items) == 1


async def test_a_failure_on_the_second_row_leaves_the_first_one_nowhere(
    backend: StorageBackend,
) -> None:
    """The property `Submission` is for: three rows land together or none of them do.

    The second submission carries a new request key and a job id that is already taken, so the
    `requests` insert succeeds and the `jobs` insert is refused. Afterwards the new request key
    must read back as nothing, which is only true if the first insert was rolled back.
    """
    await submit(backend, 0)

    doomed = make_submission(1, job=make_job(0))
    with pytest.raises(IntegrityError):
        await backend.unit_of_work.commit_submission(doomed)

    assert await backend.requests.load(request_key_for(1)) is None
    assert await backend.peek(doomed.work_item.item_id) is None


async def test_a_second_submission_by_the_same_principal_lands_beside_the_first(
    backend: StorageBackend,
) -> None:
    """There is no replay protection: two submits are two jobs (scope override item 2)."""
    await submit(backend, 0, principal_id=PRINCIPAL)
    await submit(backend, 1, principal_id=PRINCIPAL)

    page = await backend.jobs.list(demo_scope(), cursor=None, limit=10)

    assert len(page.items) == 2
    assert {job.principal_id for job in page.items} == {PRINCIPAL}
