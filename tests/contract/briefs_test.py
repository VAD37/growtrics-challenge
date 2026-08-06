"""The `briefs` table, against both backends.

Intake is its sole writer (D066) and writes each row once. `brief_hash` is the record that a run
was verified against a particular sealed brief, and it is only worth keeping while the row cannot
be edited afterwards -- so there is no update method, and a second insert under the same id is
refused rather than merged.

The round trip is the other half of what is worth asserting here. `context_items`, `constraints`
and `guard_verdict` are three jsonb columns, and a record that does not come back the way it went
in is a schema bug wearing a mapping bug's clothes.
"""

import pytest
from support.backends import StorageBackend
from support.rows import brief_id_for, make_brief

from app.storage.errors import IntegrityError


async def test_a_sealed_brief_round_trips_through_its_three_jsonb_columns(
    backend: StorageBackend,
) -> None:
    record = make_brief()

    inserted = await backend.briefs.insert(record)

    assert inserted == record
    assert await backend.briefs.load(record.brief_id) == record


async def test_the_same_brief_id_is_refused_rather_than_merged(backend: StorageBackend) -> None:
    """`uuid5(NS_BRIEF, job_id)`: a collision means one job was sealed twice."""
    await backend.briefs.insert(make_brief())

    with pytest.raises(IntegrityError):
        await backend.briefs.insert(make_brief(subject="physics"))


async def test_a_brief_that_was_never_sealed_reads_back_as_nothing(
    backend: StorageBackend,
) -> None:
    assert await backend.briefs.load(brief_id_for(4)) is None
