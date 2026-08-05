"""The `artifacts` double: what may be listed, and what may only be looked up.

`artifacts_by_principal` is a partial index over `CLEAN` and `LEARNER` rows (A6, D088), so the
listing predicate belongs to the query rather than to a filter the edge could forget. These
tests hold the double to that, and hold `get` to the opposite: it returns a quarantined row,
because `custody.store.content_stream` has to see one to refuse it.
"""

from datetime import timedelta

import pytest
from memory_rows_test import (
    OTHER_PRINCIPAL,
    PRINCIPAL,
    T0,
    artifact_id_for,
    demo_scope,
    job_id_for,
    make_artifact,
)

from app.domain.enums import Audience, ScanVerdict
from app.domain.records import Cursor
from app.storage.memory import IntegrityError, MemoryArtifactRepository, MemoryDatabase


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def artifacts(database: MemoryDatabase) -> MemoryArtifactRepository:
    return MemoryArtifactRepository(database)


async def test_insert_returns_the_row_and_stores_it(
    database: MemoryDatabase, artifacts: MemoryArtifactRepository
) -> None:
    record = make_artifact()

    inserted = await artifacts.insert(record)

    assert inserted == record
    assert database.artifacts[record.artifact_id] == record


async def test_the_same_artifact_id_is_refused_rather_than_overwritten(
    artifacts: MemoryArtifactRepository,
) -> None:
    """`uuid5(job_id | content_hash)`: a collision means the verdict was already recorded."""
    await artifacts.insert(make_artifact())

    with pytest.raises(IntegrityError):
        await artifacts.insert(make_artifact())


async def test_listing_excludes_quarantined_and_operator_rows(
    artifacts: MemoryArtifactRepository,
) -> None:
    clean = await artifacts.insert(make_artifact(0))
    await artifacts.insert(make_artifact(1, verdict=ScanVerdict.QUARANTINED))
    await artifacts.insert(make_artifact(2, audience=Audience.OPERATOR))

    page = await artifacts.list(demo_scope(), job_id=None, cursor=None, limit=10)

    assert page.items == (clean,)


async def test_a_quarantined_row_is_still_there_to_be_refused(
    artifacts: MemoryArtifactRepository,
) -> None:
    await artifacts.insert(make_artifact(1, verdict=ScanVerdict.QUARANTINED))

    found = await artifacts.get(demo_scope(), artifact_id_for(1))

    assert found is not None
    assert found.scan_verdict is ScanVerdict.QUARANTINED


async def test_listing_filters_to_one_job(artifacts: MemoryArtifactRepository) -> None:
    mine = await artifacts.insert(make_artifact(0, job_index=0))
    await artifacts.insert(make_artifact(1, job_index=1))

    page = await artifacts.list(demo_scope(), job_id=job_id_for(0), cursor=None, limit=10)

    assert page.items == (mine,)


async def test_listing_is_newest_first_and_pages_by_keyset(
    artifacts: MemoryArtifactRepository,
) -> None:
    for index in range(3):
        await artifacts.insert(make_artifact(index, created_at=T0 + timedelta(minutes=index)))

    first = await artifacts.list(demo_scope(), job_id=None, cursor=None, limit=2)
    assert [row.artifact_id for row in first.items] == [artifact_id_for(2), artifact_id_for(1)]
    assert first.next_cursor is not None

    second = await artifacts.list(
        demo_scope(), job_id=None, cursor=Cursor.decode(first.next_cursor), limit=2
    )

    assert [row.artifact_id for row in second.items] == [artifact_id_for(0)]
    assert second.next_cursor is None


async def test_reads_apply_no_ownership_predicate(artifacts: MemoryArtifactRepository) -> None:
    """@audit any caller lists and reads any artifact (scope override item 1)."""
    theirs = await artifacts.insert(make_artifact(0, principal_id=OTHER_PRINCIPAL))

    page = await artifacts.list(demo_scope(PRINCIPAL), job_id=None, cursor=None, limit=10)
    found = await artifacts.get(demo_scope(PRINCIPAL), theirs.artifact_id)

    assert page.items == (theirs,)
    assert found == theirs
