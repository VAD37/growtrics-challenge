"""The `artifacts` table, against both backends: what may be listed, and what may only be read.

`artifacts_by_principal` is a partial index over `CLEAN`, `LEARNER` rows (A6, D088), so the
listing predicate belongs to the query rather than to a filter the edge could forget. These tests
hold both adapters to that, and hold `get` to the opposite: it returns a quarantined row, because
`custody.store.content_stream` has to see one to refuse it.
"""

from datetime import timedelta

import pytest
from support.backends import StorageBackend
from support.rows import (
    OTHER_PRINCIPAL,
    PRINCIPAL,
    T0,
    artifact_id_for,
    demo_scope,
    job_id_for,
    make_artifact,
)
from support.seed import submit

from app.domain.enums import Audience, ScanVerdict
from app.domain.records import Cursor
from app.storage.errors import IntegrityError


async def test_insert_returns_the_row_and_stores_it(backend: StorageBackend) -> None:
    await submit(backend, 0)
    record = make_artifact()

    inserted = await backend.artifacts.insert(record)

    assert inserted == record
    assert await backend.artifacts.get(demo_scope(), record.artifact_id) == record


async def test_the_same_artifact_id_is_refused_rather_than_overwritten(
    backend: StorageBackend,
) -> None:
    """`uuid5(job_id | content_hash)`: a collision means the verdict was already recorded."""
    await submit(backend, 0)
    await backend.artifacts.insert(make_artifact())

    with pytest.raises(IntegrityError):
        await backend.artifacts.insert(make_artifact())


async def test_listing_excludes_quarantined_and_operator_rows(backend: StorageBackend) -> None:
    for index in range(3):
        await submit(backend, index)
    clean = await backend.artifacts.insert(make_artifact(0))
    await backend.artifacts.insert(make_artifact(1, verdict=ScanVerdict.QUARANTINED))
    await backend.artifacts.insert(make_artifact(2, audience=Audience.OPERATOR))

    page = await backend.artifacts.list(demo_scope(), job_id=None, cursor=None, limit=10)

    assert page.items == (clean,)


async def test_a_quarantined_row_is_still_there_to_be_refused(backend: StorageBackend) -> None:
    await submit(backend, 1)
    await backend.artifacts.insert(make_artifact(1, verdict=ScanVerdict.QUARANTINED))

    found = await backend.artifacts.get(demo_scope(), artifact_id_for(1))

    assert found is not None
    assert found.scan_verdict is ScanVerdict.QUARANTINED


async def test_listing_filters_to_one_job(backend: StorageBackend) -> None:
    await submit(backend, 0)
    await submit(backend, 1)
    mine = await backend.artifacts.insert(make_artifact(0, job_index=0))
    await backend.artifacts.insert(make_artifact(1, job_index=1))

    page = await backend.artifacts.list(demo_scope(), job_id=job_id_for(0), cursor=None, limit=10)

    assert page.items == (mine,)


async def test_listing_is_newest_first_and_pages_by_keyset(backend: StorageBackend) -> None:
    for index in range(3):
        await submit(backend, index)
        await backend.artifacts.insert(
            make_artifact(index, created_at=T0 + timedelta(minutes=index))
        )

    first = await backend.artifacts.list(demo_scope(), job_id=None, cursor=None, limit=2)
    assert [row.artifact_id for row in first.items] == [artifact_id_for(2), artifact_id_for(1)]
    assert first.next_cursor is not None

    second = await backend.artifacts.list(
        demo_scope(), job_id=None, cursor=Cursor.decode(first.next_cursor), limit=2
    )

    assert [row.artifact_id for row in second.items] == [artifact_id_for(0)]
    assert second.next_cursor is None


async def test_reads_apply_no_ownership_predicate(backend: StorageBackend) -> None:
    """@audit any caller lists and reads any artifact (scope override item 1)."""
    await submit(backend, 0, principal_id=OTHER_PRINCIPAL)
    theirs = await backend.artifacts.insert(make_artifact(0, principal_id=OTHER_PRINCIPAL))

    page = await backend.artifacts.list(demo_scope(PRINCIPAL), job_id=None, cursor=None, limit=10)
    found = await backend.artifacts.get(demo_scope(PRINCIPAL), theirs.artifact_id)

    assert page.items == (theirs,)
    assert found == theirs
