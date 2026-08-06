"""`work_items` over Postgres. The claim is `FOR UPDATE SKIP LOCKED`, and that is the demo.

The API drops a queue row into the same transaction as the job (`SqlUnitOfWork`), and this is
what a worker pulls it out with. Everything else in the file exists to make that claim survive
more than one worker.

**Why `SKIP LOCKED` and not a status column.** Two workers polling `WHERE claimed_by IS NULL`
both read the same oldest row; whichever updates second either overwrites the first claim or
blocks behind it until the first commits and then claims a row it has already read as free.
`FOR UPDATE SKIP LOCKED` makes the second worker step over rows the first has locked and take
the next one instead, inside one statement, with no retry loop and no advisory lock. It is the
one thing a job queue on a relational database has to get right, and Postgres has had it since
9.5.

**Why one statement and not two.** The `SELECT ... FOR UPDATE SKIP LOCKED` is a CTE feeding the
`UPDATE`, so the lock and the write are the same statement and cannot be separated by a crash,
a slow round trip, or a connection dropped in between. Two statements in one transaction would
be correct too and would hold the row lock across the network for as long as the client took to
send the second one.

**What the claim does not touch.** `claim_count` stays where it is. It counts the workers a row
has burned, and only `reclaim` can tell that from the times a row has been handed out: a claim
that ends in `complete` burned nobody. Counting here instead would spend `config.work_max_claims`
on successful runs and fail a job for having been worked on.

**What an expired lease does not do.** It does not make a row claimable. `claim` matches
`claimed_by IS NULL` and nothing else, which is also the predicate `work_items_claimable` is
partial on, so handing an abandoned row back is `reclaim`'s alone. A claim that took a lapsed row
itself would hand a poison item out forever without counting a single worker it had eaten.

"Inclusive at the deadline" is `policy.lease_expired`'s decision and this file cannot import it
into a `WHERE` clause, so `claimed_until <= %(now)s` is the second copy of a predicate that has
one owner. What keeps the two honest is the contract suite: it drives both backends over the
edge case at exactly the deadline, so a change to `lease_expired` that this file did not follow
fails here rather than in a sweep six months later.
"""

from datetime import datetime, timedelta
from typing import Final

from app.domain.ids import WorkItemId
from app.domain.records import ClaimedWorkItem
from app.orchestration.ports import Clock
from app.storage.sql.engine import SqlEngine
from app.storage.sql.rows import CLAIMED_COLUMNS, claimed_from_row

__all__ = ["SqlWorkQueue"]


class SqlWorkQueue:
    """`orchestration.ports.WorkQueue` over `work_items`."""

    def __init__(self, engine: SqlEngine, clock: Clock) -> None:
        self._engine: Final[SqlEngine] = engine
        self._clock: Final[Clock] = clock

    async def claim(self, owner: str, lease_seconds: int) -> ClaimedWorkItem | None:
        """Take the oldest available row, or `None` when the queue is empty.

        Oldest by `available_at` with `item_id` breaking ties, so the order out is the order in
        even when two rows arrived in one transaction and the dictionary order of an insert batch
        would say otherwise.

        `LIMIT 1` sits inside the CTE, before the lock: the row is chosen, locked and skipped-over
        as one decision. Moving the limit outside would lock every claimable row and then throw
        all but one of the locks away.
        """
        now = self._clock.now()
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                "WITH taken AS ("
                "    SELECT item_id FROM work_items"
                "    WHERE claimed_by IS NULL AND available_at <= %(now)s"
                "    ORDER BY available_at, item_id"
                "    LIMIT 1"
                "    FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE work_items SET claimed_by = %(owner)s, claimed_until = %(until)s "
                "WHERE item_id IN (SELECT item_id FROM taken) "
                f"RETURNING {CLAIMED_COLUMNS}",
                {
                    "now": now,
                    "owner": owner,
                    "until": now + timedelta(seconds=lease_seconds),
                },
            )
            row = await cursor.fetchone()
        return None if row is None else claimed_from_row(row)

    async def heartbeat(
        self, item_id: WorkItemId, owner: str, lease_seconds: int
    ) -> ClaimedWorkItem | None:
        """Push `claimed_until` out by another lease, while the claim is still ours.

        `None` for a row that is gone, unclaimed, or held by somebody else -- the `claimed_by`
        match in the statement is all three at once. Extending a claim we no longer hold would be
        a silent theft: the other holder is mid-run on the same job.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                "UPDATE work_items SET claimed_until = %(until)s "
                "WHERE item_id = %(item_id)s AND claimed_by = %(owner)s "
                f"RETURNING {CLAIMED_COLUMNS}",
                {
                    "until": self._clock.now() + timedelta(seconds=lease_seconds),
                    "item_id": item_id,
                    "owner": owner,
                },
            )
            row = await cursor.fetchone()
        return None if row is None else claimed_from_row(row)

    async def release(self, item_id: WorkItemId, owner: str) -> None:
        """Hand our claim back, so the row is claimable again at once.

        `claim_count` is untouched: a clean stop is not a burned worker, and a worker restarting
        must not spend `max_claims` on nothing having gone wrong. A row held by somebody else is
        left alone rather than freed, because releasing another worker's claim would put a
        running job back in the queue, which is the one thing the lease exists to prevent.
        """
        async with self._engine.connection() as connection:
            await connection.execute(
                "UPDATE work_items SET claimed_by = NULL, claimed_until = NULL "
                "WHERE item_id = %(item_id)s AND claimed_by = %(owner)s",
                {"item_id": item_id, "owner": owner},
            )

    async def complete(self, item_id: WorkItemId, owner: str) -> None:
        """Delete the row. The job is terminal and the demo never retries it (docs/demo.md D2)."""
        async with self._engine.connection() as connection:
            await connection.execute(
                "DELETE FROM work_items WHERE item_id = %(item_id)s AND claimed_by = %(owner)s",
                {"item_id": item_id, "owner": owner},
            )

    async def reclaim(self, now: datetime, max_claims: int) -> int:
        """Hand every lapsed claim back. Returns the rows made claimable again.

        `claim_count` goes up here and only here, so it counts the workers a row has burned
        rather than the times it has been run. A row already at `max_claims` is left exactly
        where it stands: handing a poison item back a fourth time is how one item eats a queue,
        and `exhausted` is where that row goes instead.

        `available_at` is not moved. A row whose turn had come keeps its place in the queue
        rather than going to the back of it because a worker died holding it.

        One statement over `work_items_lapsed`, which is partial on `claimed_by IS NOT NULL` --
        the mirror of the claim index, and the reason a sweep over a 50k backlog reads only the
        held half of it.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                "UPDATE work_items SET "
                "    claimed_by = NULL, claimed_until = NULL, claim_count = claim_count + 1 "
                "WHERE claimed_by IS NOT NULL "
                "  AND claimed_until <= %(now)s "
                "  AND claim_count < %(max_claims)s",
                {"now": now, "max_claims": max_claims},
            )
            return cursor.rowcount

    async def exhausted(self, now: datetime, max_claims: int) -> tuple[ClaimedWorkItem, ...]:
        """The lapsed rows `reclaim` will not touch, oldest first.

        `now` is what keeps this off a row a live worker is still holding. A row on its last
        claim whose lease has not lapsed is on its final attempt, not past it, and failing the
        job under a worker that is still running it would be the sweep racing the runner.

        `claim_count >= max_claims` is the exact complement of `reclaim`'s `<`, so no lapsed row
        is in both sets and none is in neither. Ordered like `claim`, so two ticks over the same
        backlog give up in the same order.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {CLAIMED_COLUMNS} FROM work_items "
                "WHERE claimed_by IS NOT NULL "
                "  AND claimed_until <= %(now)s "
                "  AND claim_count >= %(max_claims)s "
                "ORDER BY available_at, item_id",
                {"now": now, "max_claims": max_claims},
            )
            rows = await cursor.fetchall()
        return tuple(claimed_from_row(row) for row in rows)

    async def discard(self, item_id: WorkItemId) -> bool:
        """Delete a row whoever holds it. `True` when a row went.

        No owner to match, unlike `complete`: a swept row is either unclaimed or held by a
        process that is not coming back, and there is nobody left to ask. A `FAILED` job whose
        queue row outlived it is a row the next worker claims and the runner then drops, forever.
        """
        async with self._engine.connection() as connection:
            cursor = await connection.execute(
                "DELETE FROM work_items WHERE item_id = %(item_id)s",
                {"item_id": item_id},
            )
            return cursor.rowcount > 0
