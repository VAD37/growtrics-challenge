"""`WorkQueue`, against both backends. The claim is the centre of the demo.

The rows are submitted deliberately out of order. A dictionary that preserved insertion order
would pass a test that inserted them in order and then hand a worker the wrong job the first time
two submits landed together or an item was backdated; on the SQL side the same test would pass on
whatever order the heap happened to return.

The second half is what the sweep reads. `reclaim` hands a lapsed row back and counts the worker
it burned, `exhausted` names the lapsed rows it refuses to hand back again, and `discard` removes
a row with no owner to ask. `max_claims` is the whole boundary between the first two, so every
test that crosses it asserts both sides at once.

The last test is the one only Postgres can fail: five workers claiming at once must come away
with five different rows. That is `FOR UPDATE SKIP LOCKED` doing its job, and a `SELECT` followed
by an `UPDATE` would hand the same row to two of them.
"""

import asyncio
from datetime import timedelta
from typing import Final

from support.backends import StorageBackend
from support.rows import (
    LEASE_SECONDS,
    OTHER_OWNER,
    OWNER,
    T0,
    work_item_id_for,
)
from support.seed import submit

MAX_CLAIMS: Final[int] = 3
"""`config.work_max_claims` in the tests that need one. A row at this count is `exhausted`'s."""

NEVER_EXHAUSTED: Final[int] = 1_000
"""A `max_claims` no seeded row reaches, for the tests that are not about the boundary."""


async def seed(backend: StorageBackend, index: int, *, minutes: int = 0) -> None:
    """A queue row due `minutes` after `T0`, through the transaction that writes one."""
    await submit(backend, index, created_at=T0 + timedelta(minutes=minutes))


async def seed_held(
    backend: StorageBackend,
    index: int,
    *,
    lease_left: int,
    claim_count: int = 1,
    owner: str = OWNER,
) -> None:
    """A row in the state a dead worker leaves behind: held, with a lease that runs out.

    Held through the backend's escape hatch rather than by claiming it, so the claim count under
    test is the one the test states. What a lapsed row does next depends on that number and on
    nothing else.
    """
    await seed(backend, index)
    await backend.hold(
        work_item_id_for(index), owner=owner, lease_left=lease_left, claim_count=claim_count
    )


# --------------------------------------------------------------------------- claiming


async def test_an_empty_queue_hands_back_nothing(backend: StorageBackend) -> None:
    assert await backend.queue.claim(OWNER, LEASE_SECONDS) is None


async def test_the_oldest_available_row_is_claimed_whatever_order_it_arrived_in(
    backend: StorageBackend,
) -> None:
    await seed(backend, 2, minutes=20)
    await seed(backend, 0, minutes=0)
    await seed(backend, 1, minutes=10)
    backend.clock.advance(3600)

    claimed = [await backend.queue.claim(OWNER, LEASE_SECONDS) for _ in range(3)]

    assert [item.item_id for item in claimed if item is not None] == [
        work_item_id_for(0),
        work_item_id_for(1),
        work_item_id_for(2),
    ]


async def test_a_row_that_is_not_due_yet_is_not_claimed(backend: StorageBackend) -> None:
    await seed(backend, 0, minutes=10)

    assert await backend.queue.claim(OWNER, LEASE_SECONDS) is None


async def test_a_claim_holds_a_lease_and_hides_the_row_from_everyone_else(
    backend: StorageBackend,
) -> None:
    await seed(backend, 0)

    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)

    assert claimed is not None
    assert claimed.claimed_by == OWNER
    assert claimed.claimed_until == T0 + timedelta(seconds=LEASE_SECONDS)
    # Still zero. A row being worked on has burned nobody yet; only `reclaim` counts.
    assert claimed.claim_count == 0
    assert await backend.queue.claim(OTHER_OWNER, LEASE_SECONDS) is None


async def test_a_lapsed_lease_is_reclaimed_before_anybody_else_can_have_it(
    backend: StorageBackend,
) -> None:
    """A lapsed row is not simply up for grabs: the sweep hands it back, and only then.

    That order is what makes `max_claims` count anything. A claim that took a lapsed row itself
    would hand a poison item out forever without ever noticing it had.
    """
    await seed(backend, 0)
    await backend.queue.claim(OWNER, LEASE_SECONDS)
    backend.clock.advance(LEASE_SECONDS * 10)

    assert await backend.queue.claim(OTHER_OWNER, LEASE_SECONDS) is None
    row = await backend.peek(work_item_id_for(0))
    assert row is not None
    assert row.claimed_by == OWNER

    assert await backend.queue.reclaim(backend.clock.now(), MAX_CLAIMS) == 1

    taken = await backend.queue.claim(OTHER_OWNER, LEASE_SECONDS)
    assert taken is not None
    assert taken.claimed_by == OTHER_OWNER


async def test_concurrent_claims_never_hand_the_same_row_to_two_workers(
    backend: StorageBackend,
) -> None:
    """`FOR UPDATE SKIP LOCKED`, from the outside. Five workers, five rows, no overlap."""
    workers = 5
    for index in range(workers):
        await seed(backend, index)

    claimed = await asyncio.gather(
        *(backend.queue.claim(f"worker-{index}", LEASE_SECONDS) for index in range(workers))
    )

    taken = [item.item_id for item in claimed if item is not None]
    assert len(taken) == workers
    assert len(set(taken)) == workers


# --------------------------------------------------------------------------- holding


async def test_a_heartbeat_extends_a_claim_that_is_still_ours(backend: StorageBackend) -> None:
    await seed(backend, 0)
    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None
    backend.clock.advance(30)

    renewed = await backend.queue.heartbeat(claimed.item_id, OWNER, LEASE_SECONDS)

    assert renewed is not None
    assert renewed.claimed_until == T0 + timedelta(seconds=30 + LEASE_SECONDS)
    assert renewed.claim_count == claimed.claim_count


async def test_a_heartbeat_from_somebody_else_is_refused_and_changes_nothing(
    backend: StorageBackend,
) -> None:
    await seed(backend, 0)
    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    assert await backend.queue.heartbeat(claimed.item_id, OTHER_OWNER, LEASE_SECONDS) is None
    row = await backend.peek(claimed.item_id)
    assert row is not None
    assert row.claimed_until == claimed.claimed_until


async def test_a_heartbeat_on_a_row_that_is_gone_is_refused(backend: StorageBackend) -> None:
    assert await backend.queue.heartbeat(work_item_id_for(0), OWNER, LEASE_SECONDS) is None


async def test_release_hands_the_row_straight_back(backend: StorageBackend) -> None:
    await seed(backend, 0)
    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await backend.queue.release(claimed.item_id, OWNER)

    row = await backend.peek(claimed.item_id)
    assert row is not None
    assert row.claimed_by is None
    # A clean stop is not a burned worker. `release` costs the row nothing, and neither does the
    # claim after it, or a worker restarting would spend `max_claims` on nothing going wrong.
    assert row.claim_count == 0
    reclaimed = await backend.queue.claim(OTHER_OWNER, LEASE_SECONDS)
    assert reclaimed is not None
    assert reclaimed.claim_count == 0


async def test_release_by_a_stranger_leaves_the_claim_alone(backend: StorageBackend) -> None:
    """Freeing another worker's claim would put a running job back in the queue."""
    await seed(backend, 0)
    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await backend.queue.release(claimed.item_id, OTHER_OWNER)

    row = await backend.peek(claimed.item_id)
    assert row is not None
    assert row.claimed_by == OWNER


async def test_complete_removes_the_row(backend: StorageBackend) -> None:
    await seed(backend, 0)
    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await backend.queue.complete(claimed.item_id, OWNER)

    assert await backend.peek(claimed.item_id) is None


async def test_complete_by_a_stranger_removes_nothing(backend: StorageBackend) -> None:
    await seed(backend, 0)
    claimed = await backend.queue.claim(OWNER, LEASE_SECONDS)
    assert claimed is not None

    await backend.queue.complete(claimed.item_id, OTHER_OWNER)

    assert await backend.peek(claimed.item_id) is not None


# --------------------------------------------------------------------------- what the sweep reads


async def test_reclaim_hands_a_lapsed_row_back_and_counts_the_worker_it_burned(
    backend: StorageBackend,
) -> None:
    """`claim_count` moves here and nowhere else, so it counts holders that never came back."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS)
    backend.clock.advance(LEASE_SECONDS * 2)

    assert await backend.queue.reclaim(backend.clock.now(), MAX_CLAIMS) == 1

    handed_back = await backend.peek(work_item_id_for(0))
    assert handed_back is not None
    assert handed_back.claimed_by is None
    assert handed_back.claimed_until is None
    assert handed_back.claim_count == 2
    # Its turn had already come. A dead holder must not send it to the back of the queue.
    assert handed_back.available_at == T0
    assert await backend.queue.claim(OTHER_OWNER, LEASE_SECONDS) is not None


async def test_reclaim_leaves_a_live_lease_alone(backend: StorageBackend) -> None:
    """A worker holding a lease is a worker still working."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS)
    backend.clock.advance(LEASE_SECONDS - 1)

    assert await backend.queue.reclaim(backend.clock.now(), MAX_CLAIMS) == 0
    row = await backend.peek(work_item_id_for(0))
    assert row is not None
    assert row.claimed_by == OWNER


async def test_a_lease_running_out_exactly_now_has_lapsed(backend: StorageBackend) -> None:
    """Inclusive at the deadline, like `policy.lease_expired`: at the edge the holder is late."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS)
    backend.clock.advance(LEASE_SECONDS)

    assert await backend.queue.reclaim(backend.clock.now(), MAX_CLAIMS) == 1


async def test_reclaim_leaves_a_worn_out_row_where_it_is_and_exhausted_names_it(
    backend: StorageBackend,
) -> None:
    """Handing a poison item back for the fourth time is a queue that never drains."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS, claim_count=MAX_CLAIMS)
    await seed_held(backend, 1, lease_left=LEASE_SECONDS, claim_count=1)
    backend.clock.advance(LEASE_SECONDS * 2)

    assert await backend.queue.reclaim(backend.clock.now(), MAX_CLAIMS) == 1

    spent = await backend.peek(work_item_id_for(0))
    still_worth_trying = await backend.peek(work_item_id_for(1))
    assert spent is not None and still_worth_trying is not None
    assert spent.claimed_by == OWNER
    assert spent.claim_count == MAX_CLAIMS
    assert still_worth_trying.claimed_by is None

    exhausted = await backend.queue.exhausted(backend.clock.now(), MAX_CLAIMS)
    assert [item.item_id for item in exhausted] == [work_item_id_for(0)]
    assert exhausted[0].claim_count == MAX_CLAIMS


async def test_exhausted_passes_over_a_worn_out_row_a_worker_is_still_holding(
    backend: StorageBackend,
) -> None:
    """Last claim, still beating. It is on its final attempt, not past it."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS, claim_count=MAX_CLAIMS)
    backend.clock.advance(LEASE_SECONDS - 1)

    assert await backend.queue.exhausted(backend.clock.now(), MAX_CLAIMS) == ()


async def test_reclaiming_twice_hands_the_same_row_back_once(backend: StorageBackend) -> None:
    """All zeros is what the second of two identical ticks has to return."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS)
    backend.clock.advance(LEASE_SECONDS * 2)

    assert await backend.queue.reclaim(backend.clock.now(), NEVER_EXHAUSTED) == 1
    assert await backend.queue.reclaim(backend.clock.now(), NEVER_EXHAUSTED) == 0
    row = await backend.peek(work_item_id_for(0))
    assert row is not None
    assert row.claim_count == 2


async def test_discard_removes_the_row_whoever_holds_it(backend: StorageBackend) -> None:
    """No owner to match: the holder is a process that is not coming back."""
    await seed_held(backend, 0, lease_left=LEASE_SECONDS, owner=OTHER_OWNER)

    assert await backend.queue.discard(work_item_id_for(0)) is True
    assert await backend.peek(work_item_id_for(0)) is None
    assert await backend.queue.discard(work_item_id_for(0)) is False
