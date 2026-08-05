"""`work_items` in memory: claim, heartbeat, release, complete, and the three the sweep reads.

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

What a crash leaves behind is `engine/sweeper.py`'s, and it splits three ways here. `reclaim`
hands a lapsed row back so the next worker can have it, `exhausted` names the lapsed rows it will
not hand back because they have burned every worker they are going to get, and `discard` removes
a row whoever holds it. The boundary between the first two is `max_claims` and nothing else, so
no row is both, and every lapsed row is one or the other.

`lease_expired` is imported rather than restated. "Inclusive at the deadline" is a decision, and a
second copy of `<=` here would be a decision nobody knows they are making the day it changes.
"""

from dataclasses import replace
from datetime import datetime, timedelta

from app.domain.ids import WorkItemId
from app.domain.records import ClaimedWorkItem
from app.orchestration.engine.policy import lease_expired
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
        """Hand every lapsed claim back. Returns the rows made claimable again.

        `claim_count` goes up here and only here, so it counts the workers a row has burned
        rather than the times it has been run. A row already at `max_claims` is left exactly as
        it stands: handing a poison item back a fourth time is how one item eats a queue, and
        `exhausted` is where that row goes instead.

        The claim is cleared through `released()`, so "handed back" is one shape wherever it
        happens, and `available_at` is left alone -- a row whose turn had come keeps its place in
        the queue instead of going to the back of it because a worker died holding it.
        """
        handed_back = 0
        for item_id, row in list(self._database.work_items.items()):
            if not self._lapsed(row, now) or row.claim_count >= max_claims:
                continue
            self._database.work_items[item_id] = replace(
                row.released(), claim_count=row.claim_count + 1
            )
            handed_back += 1
        return handed_back

    async def exhausted(self, now: datetime, max_claims: int) -> tuple[ClaimedWorkItem, ...]:
        """The lapsed rows `reclaim` will not touch, oldest first.

        `now` is what keeps this off a row a live worker is still holding. A row on its last
        claim whose lease has not lapsed is on its final attempt, not past it, and failing the
        job under a worker that is still running it would be the sweep racing the runner.

        Ordered like `claim` so two ticks over the same backlog give up in the same order.
        """
        lapsed = [
            row
            for row in self._database.work_items.values()
            if self._lapsed(row, now) and row.claim_count >= max_claims
        ]
        lapsed.sort(key=lambda row: (row.available_at, row.item_id))
        return tuple(row.as_claimed() for row in lapsed)

    async def discard(self, item_id: WorkItemId) -> bool:
        """Delete a row whoever holds it. `True` when a row went.

        No owner to match, unlike `complete`: a swept row is either unclaimed or held by a
        process that is not coming back, and there is nobody left to ask. A `FAILED` job whose
        queue row outlived it is a row the next worker claims and the runner then drops, forever.
        """
        return self._database.work_items.pop(item_id, None) is not None

    @staticmethod
    def _lapsed(row: WorkItemRow, now: datetime) -> bool:
        """Held, and past its deadline. An unclaimed row has no lease to lapse."""
        return (
            row.claimed_by is not None
            and row.claimed_until is not None
            and lease_expired(row.claimed_until, now)
        )

    def rows(self) -> tuple[WorkItemRow, ...]:
        """Every row with its claim state, oldest first. For assertions, not for the ports."""
        return tuple(
            sorted(
                self._database.work_items.values(),
                key=lambda row: (row.available_at, row.item_id),
            )
        )
