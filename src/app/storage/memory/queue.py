"""`work_items` in memory: claim, heartbeat, release, complete.

The SQL adapter claims with `FOR UPDATE SKIP LOCKED`; this one sorts. Both have to hand out the
oldest available row, and a dictionary that happens to preserve insertion order is not a queue --
rows arrive out of order the moment two submits land in the same transaction batch, and an item
whose `available_at` was backdated has to come first regardless of when it was inserted. So the
order is computed on every claim, from `(available_at, item_id)`, and never assumed.

The lease discipline mirrors `app/worker.py` exactly:

* `heartbeat` extends the lease only while the claim is still ours. A `None` tells the worker its
  claim is gone, which is the signal that stops it from beating on over somebody else's row.
* `release` hands a claim back and makes the row immediately claimable. Only a clean stop between
  claiming and starting calls it; a crash must not, because an abandoned lease has to expire.
* `complete` deletes the row. The demo never retries (`docs/demo.md`, D2).
* `reclaim` is deferred, and the docstring says what that costs.
"""

from datetime import datetime, timedelta

from app.domain.ids import WorkItemId
from app.domain.records import ClaimedWorkItem
from app.orchestration.ports import Clock
from app.storage.memory.state import MemoryDatabase, WorkItemRow

__all__ = ["MemoryWorkQueue"]


class MemoryWorkQueue:
    """`WorkQueue` over `MemoryDatabase.work_items`."""

    def __init__(self, database: MemoryDatabase, clock: Clock) -> None:
        self._database: MemoryDatabase = database
        self._clock: Clock = clock

    async def claim(self, owner: str, lease_seconds: int) -> ClaimedWorkItem | None:
        """Take the oldest available row, or `None` when the queue is empty.

        Oldest by `available_at` with the item id breaking ties, so the order out is the order
        in even when the rows were not inserted in it.
        """
        now = self._clock.now()
        claimable = [row for row in self._database.work_items.values() if row.is_claimable(now)]
        if not claimable:
            return None
        oldest = min(claimable, key=lambda row: (row.available_at, row.item_id))
        held = oldest.claimed(
            owner=owner,
            until=now + timedelta(seconds=lease_seconds),
            claim_count=oldest.claim_count + 1,
        )
        self._database.work_items[held.item_id] = held
        return held.as_claimed()

    async def heartbeat(
        self, item_id: WorkItemId, owner: str, lease_seconds: int
    ) -> ClaimedWorkItem | None:
        """Push `claimed_until` out by another lease, while the claim is still ours.

        `None` for a row that is gone, unclaimed, or held by somebody else. Extending a claim we
        no longer hold would be a silent theft: the other holder is mid-run on the same job.
        """
        row = self._database.work_items.get(item_id)
        if row is None or not row.held_by(owner):
            return None
        renewed = row.claimed(
            owner=owner,
            until=self._clock.now() + timedelta(seconds=lease_seconds),
            claim_count=row.claim_count,
        )
        self._database.work_items[item_id] = renewed
        return renewed.as_claimed()

    async def release(self, item_id: WorkItemId, owner: str) -> None:
        """Hand our claim back, so the row is claimable again at once.

        A row held by somebody else is left alone rather than freed. Releasing another worker's
        claim would put a running job back in the queue, which is the one thing the lease is for.
        """
        row = self._database.work_items.get(item_id)
        if row is None or not row.held_by(owner):
            return
        self._database.work_items[item_id] = row.released()

    async def complete(self, item_id: WorkItemId, owner: str) -> None:
        """Delete the row. The job is terminal and the demo never retries it."""
        row = self._database.work_items.get(item_id)
        if row is None or not row.held_by(owner):
            return
        del self._database.work_items[item_id]

    async def reclaim(self, now: datetime, max_claims: int) -> int:
        """@TODO DEFERRED, scope override item 8. Nothing calls this.

        It would clear `claimed_by` and `claimed_until` on every row whose `claimed_until` is
        before `now`, increment `claim_count`, and drop the rows past `max_claims`. Everything it
        needs is already on `WorkItemRow`, which is why the deferral costs no schema change.

        **Until it exists, an abandoned item stays claimed forever and its job sits at
        `RUNNING`.** `claim` deliberately refuses to pick up an expired lease, so nothing else in
        this class quietly implements half of this and hides the gap. See
        `app/orchestration/engine/sweeper.py`.
        """
        raise NotImplementedError("queue reclaim is deferred; see the docstring")

    def rows(self) -> tuple[WorkItemRow, ...]:
        """Every row with its claim state, oldest first. For assertions, not for the ports."""
        return tuple(
            sorted(
                self._database.work_items.values(),
                key=lambda row: (row.available_at, row.item_id),
            )
        )
