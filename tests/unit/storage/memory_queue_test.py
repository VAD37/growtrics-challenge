"""The queue double: FIFO out, whatever order went in, and a lease that means something.

The rows are inserted deliberately out of order. A dictionary that preserved insertion order
would pass a test that inserted them in order, and then hand a worker the wrong job the first
time two submits landed together or an item was backdated.

The second half is what the sweep reads. `reclaim` hands a lapsed row back and counts the worker
it burned, `exhausted` names the lapsed rows it refuses to hand back again, and `discard` removes
a row with no owner to ask. `max_claims` is the whole boundary between the first two, so every
test below that crosses it asserts both sides of it at once.
"""

from datetime import timedelta
from typing import Final

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

MAX_CLAIMS: Final[int] = 3
"""`config.work_max_claims` in the tests that need one. A row at this count is `exhausted`'s."""


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


def seed_held(
    database: MemoryDatabase,
    index: int,
    *,
    lease_left: int,
    claim_count: int = 1,
    owner: str = OWNER,
) -> WorkItemRow:
    """A row in the state a dead worker leaves behind: held, with a lease that runs out.

    Seeded rather than claimed through the queue so the claim count under test is the one the
    test states. What a lapsed row does next depends on that number and on nothing else.
    """
    row = WorkItemRow.queued(make_queued_item(index)).claimed(
        owner=owner,
        until=T0 + timedelta(seconds=lease_left),
        claim_count=claim_count,
    )
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
    # Still zero. A row being worked on has burned nobody yet; only `reclaim` counts.
    assert claimed.claim_count == 0
    assert await queue.claim(OTHER_OWNER, LEASE_SECONDS) is None


async def test_a_lapsed_lease_is_reclaimed_before_anybody_else_can_have_it(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """A lapsed row is not simply up for grabs: the sweep hands it back, and only then.

    That order is what makes `max_claims` count anything. A claim that took a lapsed row itself
    would hand a poison item out forever without ever noticing it had.
    """
    seed(database, 0, minutes=0)
    await queue.claim(OWNER, LEASE_SECONDS)
    clock.advance(LEASE_SECONDS * 10)

    assert await queue.claim(OTHER_OWNER, LEASE_SECONDS) is None
    assert database.work_items[work_item_id_for(0)].claimed_by == OWNER

    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 1

    taken = await queue.claim(OTHER_OWNER, LEASE_SECONDS)
    assert taken is not None
    assert taken.claimed_by == OTHER_OWNER


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
    # A clean stop is not a burned worker. `release` costs the row nothing, and neither does the
    # claim after it, or a worker restarting would spend `max_claims` on nothing going wrong.
    assert row.claim_count == 0
    reclaimed = await queue.claim(OTHER_OWNER, LEASE_SECONDS)
    assert reclaimed is not None
    assert reclaimed.claim_count == 0


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


async def test_reclaim_hands_a_lapsed_row_back_and_counts_the_worker_it_burned(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """`claim_count` moves here and nowhere else, so it counts holders that never came back."""
    row = seed_held(database, 0, lease_left=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS * 2)

    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 1

    handed_back = database.work_items[row.item_id]
    assert handed_back.claimed_by is None
    assert handed_back.claimed_until is None
    assert handed_back.claim_count == 2
    # Its turn had already come. A dead holder must not send it to the back of the queue.
    assert handed_back.available_at == row.available_at
    assert await queue.claim(OTHER_OWNER, LEASE_SECONDS) is not None


async def test_reclaim_leaves_a_live_lease_alone(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """A worker holding a lease is a worker still working."""
    seed_held(database, 0, lease_left=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS - 1)

    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 0
    assert database.work_items[work_item_id_for(0)].claimed_by == OWNER


async def test_a_lease_running_out_exactly_now_has_lapsed(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """Inclusive at the deadline, like `policy.lease_expired`: at the edge the holder is late."""
    seed_held(database, 0, lease_left=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS)

    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 1


async def test_reclaim_leaves_a_worn_out_row_where_it_is_and_exhausted_names_it(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """Handing a poison item back for the fourth time is a queue that never drains."""
    spent = seed_held(database, 0, lease_left=LEASE_SECONDS, claim_count=MAX_CLAIMS)
    still_worth_trying = seed_held(database, 1, lease_left=LEASE_SECONDS, claim_count=1)
    clock.advance(LEASE_SECONDS * 2)

    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 1

    assert database.work_items[spent.item_id].claimed_by == OWNER
    assert database.work_items[spent.item_id].claim_count == MAX_CLAIMS
    assert database.work_items[still_worth_trying.item_id].claimed_by is None

    exhausted = await queue.exhausted(clock.now(), MAX_CLAIMS)
    assert [item.item_id for item in exhausted] == [spent.item_id]
    assert exhausted[0].claim_count == MAX_CLAIMS


async def test_exhausted_passes_over_a_worn_out_row_a_worker_is_still_holding(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """Last claim, still beating. It is on its final attempt, not past it."""
    seed_held(database, 0, lease_left=LEASE_SECONDS, claim_count=MAX_CLAIMS)
    clock.advance(LEASE_SECONDS - 1)

    assert await queue.exhausted(clock.now(), MAX_CLAIMS) == ()


async def test_reclaiming_twice_hands_the_same_row_back_once(
    database: MemoryDatabase, queue: MemoryWorkQueue, clock: FrozenClock
) -> None:
    """All zeros is what the second of two identical ticks has to return."""
    seed_held(database, 0, lease_left=LEASE_SECONDS)
    clock.advance(LEASE_SECONDS * 2)

    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 1
    assert await queue.reclaim(clock.now(), MAX_CLAIMS) == 0
    assert database.work_items[work_item_id_for(0)].claim_count == 2


async def test_discard_removes_the_row_whoever_holds_it(
    database: MemoryDatabase, queue: MemoryWorkQueue
) -> None:
    """No owner to match: the holder is a process that is not coming back."""
    seed_held(database, 0, lease_left=LEASE_SECONDS, owner=OTHER_OWNER)

    assert await queue.discard(work_item_id_for(0)) is True
    assert database.work_items == {}
    assert await queue.discard(work_item_id_for(0)) is False
