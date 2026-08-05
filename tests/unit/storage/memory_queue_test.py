"""The queue double: FIFO out, whatever order went in, and a lease that means something.

The rows are inserted deliberately out of order. A dictionary that preserved insertion order
would pass a test that inserted them in order, and then hand a worker the wrong job the first
time two submits landed together or an item was backdated.
"""

from datetime import timedelta

import pytest
from memory_rows_test import (
    LEASE_SECONDS,
    OTHER_OWNER,
    OWNER,
    T0,
    make_queued_item,
    work_item_id_for,
)

from app.storage.memory import (
    FrozenClock,
    MemoryDatabase,
    MemoryWorkQueue,
    WorkItemRow,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(at=T0)


@pytest.fixture
def database() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def queue(database: MemoryDatabase, clock: FrozenClock) -> MemoryWorkQueue:
    return MemoryWorkQueue(database, clock)


def seed(database: MemoryDatabase, index: int, *, minutes: int) -> WorkItemRow:
    item = make_queued_item(index, available_at=T0 + timedelta(minutes=minutes))
    row = WorkItemRow.queued(item)
    database.work_items[row.item_id] = row
    return row


async def test_an_empty_queue_hands_back_nothing(queue: MemoryWorkQueue) -> None:
    assert await queue.claim(OWNER, LEASE_SECONDS) is None


async def test_the_oldest_available_row_is_claimed_whatever_order_it_arrived_in(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    seed(database, 2, minutes=20)
    seed(database, 0, minutes=0)
    seed(database, 1, minutes=10)
    clock.advance(3600)

    claimed = [await queue.claim(OWNER, LEASE_SECONDS) for _ in range(3)]

    assert [item.item_id for item in claimed if item is not None] == [
        work_item_id_for(0),
        work_item_id_for(1),
        work_item_id_for(2),
    ]


async def test_a_row_that_is_not_due_yet_is_not_claimed(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    seed(database, 0, minutes=10)

    assert await queue.claim(OWNER, LEASE_SECONDS) is None


async def test_a_claim_holds_a_lease_and_hides_the_row_from_everyone_else(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    seed(database, 0, minutes=0)

    claimed = await queue.claim(OWNER, LEASE_SECONDS)

    assert claimed is not None
    assert claimed.claimed_by == OWNER
    assert claimed.claimed_until == T0 + timedelta(seconds=LEASE_SECONDS)
    assert claimed.claim_count == 1
    assert await queue.claim(OTHER_OWNER, LEASE_SECONDS) is None


async def test_an_expired_lease_stays_claimed_because_reclaim_is_deferred(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """The gap `reclaim` fills, asserted so nobody has to take the docstring's word for it."""
    seed(database, 0, minutes=0)
    await queue.claim(OWNER, LEASE_SECONDS)
    clock.advance(LEASE_SECONDS * 10)

    assert await queue.claim(OTHER_OWNER, LEASE_SECONDS) is None
    assert database.work_items[work_item_id_for(0)].claimed_by == OWNER


async def test_a_heartbeat_extends_a_claim_that_is_still_ours(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    seed(database, 0, minutes=0)
    claimed = await queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None
    clock.advance(30)

    renewed = await queue.heartbeat(claimed.item_id, OWNER, LEASE_SECONDS)

    assert renewed is not None
    assert renewed.claimed_until == T0 + timedelta(seconds=30 + LEASE_SECONDS)
    assert renewed.claim_count == claimed.claim_count


async def test_a_heartbeat_from_somebody_else_is_refused_and_changes_nothing(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    seed(database, 0, minutes=0)
    claimed = await queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    assert await queue.heartbeat(claimed.item_id, OTHER_OWNER, LEASE_SECONDS) is None
    assert database.work_items[claimed.item_id].claimed_until == claimed.claimed_until


async def test_a_heartbeat_on_a_row_that_is_gone_is_refused(queue: MemoryWorkQueue) -> None:
    assert await queue.heartbeat(work_item_id_for(0), OWNER, LEASE_SECONDS) is None


async def test_release_hands_the_row_straight_back(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    seed(database, 0, minutes=0)
    claimed = await queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await queue.release(claimed.item_id, OWNER)

    row = database.work_items[claimed.item_id]
    assert row.claimed_by is None
    assert row.claim_count == 1
    reclaimed = await queue.claim(OTHER_OWNER, LEASE_SECONDS)
    assert reclaimed is not None
    assert reclaimed.claim_count == 2


async def test_release_by_a_stranger_leaves_the_claim_alone(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    """Freeing another worker's claim would put a running job back in the queue."""
    seed(database, 0, minutes=0)
    claimed = await queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await queue.release(claimed.item_id, OTHER_OWNER)

    assert database.work_items[claimed.item_id].claimed_by == OWNER


async def test_complete_removes_the_row(database: MemoryDatabase, queue: MemoryWorkQueue) -> None:
    seed(database, 0, minutes=0)
    claimed = await queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await queue.complete(claimed.item_id, OWNER)

    assert database.work_items == {}


async def test_complete_by_a_stranger_removes_nothing(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    seed(database, 0, minutes=0)
    claimed = await queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await queue.complete(claimed.item_id, OTHER_OWNER)

    assert claimed.item_id in database.work_items


async def test_reclaim_refuses_to_pretend(queue: MemoryWorkQueue, clock: FrozenClock) -> None:
    """A deferred method that returned zero would look like a sweep that found nothing."""
    with pytest.raises(NotImplementedError):
        await queue.reclaim(clock.now(), 3)
