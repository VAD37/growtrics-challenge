"""The rows the in-memory doubles share, and the failures they are allowed to have.

One `MemoryDatabase` object per test or per smoke run, handed to every double, because the
doubles are not independent: `UnitOfWork` writes the row `JobRepository` reads and the row
`WorkQueue` claims. Five separate dictionaries behind five separate classes would pass their own
unit tests and fail the moment a job had to move.

The tables are the six of `docs/demo.md` minus `briefs` and `idempotency_keys`: briefs are
`intake`'s to write and there is no idempotency key in the demo (scope override item 2). Bytes
are not here at all -- `MemoryObjectStore` holds those, in a different object, because a store
that kept files beside rows would make D052 look like a formality.

The three errors are the three ways a real database says no. They are named rather than folded
into one so a test asserts on the failure it meant: a duplicate primary key is a caller inserting
twice, a version conflict is a lost update, and a missing row is a caller acting on something
that was deleted underneath it.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.access import Principal
from app.domain.ids import ArtifactId, JobId, PrincipalId, RequestKey, WorkItemId
from app.domain.records import (
    ArtifactRecord,
    ClaimedWorkItem,
    Cursor,
    JobRecord,
    Page,
    StoredRequest,
)
from app.orchestration.ports import QueuedWorkItem

__all__ = [
    "IntegrityError",
    "MemoryDatabase",
    "MemoryDatabaseProbe",
    "MemoryStorageError",
    "RowNotFoundError",
    "SortKey",
    "VersionConflictError",
    "WorkItemRow",
    "keyset_page",
]


class MemoryStorageError(RuntimeError):
    """Base for every refusal these doubles make. Never raised directly."""


class IntegrityError(MemoryStorageError):
    """A second row under a primary key that already has one."""


class VersionConflictError(MemoryStorageError):
    """A write whose `expected_version` no longer matches the row.

    The demo runs one worker, so this is a bug rather than contention -- which is exactly why it
    is loud. A double that quietly applied the write would turn a lost update into a wrong
    status nobody could trace back here.
    """


class RowNotFoundError(MemoryStorageError):
    """A write against a row that is not there."""


@dataclass(frozen=True, slots=True)
class WorkItemRow:
    """A `work_items` row with its claim state, which neither queue record carries alone.

    `QueuedWorkItem` is the shape at insert and `ClaimedWorkItem` is the shape under a lease.
    The row is both plus the transition between them, so the claim columns live here and the
    domain keeps its two clean views of it.
    """

    item_id: WorkItemId
    job_id: JobId
    available_at: datetime
    created_at: datetime
    claimed_by: str | None = None
    claimed_until: datetime | None = None
    claim_count: int = 0

    @classmethod
    def queued(cls, item: QueuedWorkItem) -> WorkItemRow:
        """The row a submit inserts: available now, claimed by nobody."""
        return cls(
            item_id=item.item_id,
            job_id=item.job_id,
            available_at=item.available_at,
            created_at=item.created_at,
        )

    def is_claimable(self, now: datetime) -> bool:
        """Unclaimed and due.

        An expired lease is deliberately NOT claimable. Handing an abandoned row back is
        `WorkQueue.reclaim`'s, and the sweep is the only thing allowed to decide a holder is
        gone: a claim that took a lapsed row itself would hand it out without counting the worker
        it burned, and `max_claims` would then never stop a poison item. This mirrors the
        `work_items_claimable` index, which is `WHERE claimed_by IS NULL` and nothing else.
        """
        return self.claimed_by is None and self.available_at <= now

    def held_by(self, owner: str) -> bool:
        return self.claimed_by == owner

    def claimed(self, *, owner: str, until: datetime, claim_count: int) -> WorkItemRow:
        return WorkItemRow(
            item_id=self.item_id,
            job_id=self.job_id,
            available_at=self.available_at,
            created_at=self.created_at,
            claimed_by=owner,
            claimed_until=until,
            claim_count=claim_count,
        )

    def released(self) -> WorkItemRow:
        """The row handed back: claim cleared, `claim_count` kept as evidence it was tried."""
        return WorkItemRow(
            item_id=self.item_id,
            job_id=self.job_id,
            available_at=self.available_at,
            created_at=self.created_at,
            claim_count=self.claim_count,
        )

    def as_claimed(self) -> ClaimedWorkItem:
        """The domain view of a held row. Only valid while somebody holds it."""
        if self.claimed_by is None or self.claimed_until is None:
            raise RowNotFoundError(f"work item {self.item_id} is not claimed")
        return ClaimedWorkItem(
            item_id=self.item_id,
            job_id=self.job_id,
            claimed_by=self.claimed_by,
            claimed_until=self.claimed_until,
            claim_count=self.claim_count,
        )


@dataclass(slots=True)
class MemoryDatabase:
    """Every table the demo's doubles write, in one object.

    Each table is reassigned rather than mutated by `MemoryUnitOfWork`, which is what makes a
    transaction work: the unit of work builds copies, fails wherever it was told to, and swaps
    all three in only if it reached the end.
    """

    principals: dict[PrincipalId, Principal] = field(default_factory=dict)
    requests: dict[RequestKey, StoredRequest] = field(default_factory=dict)
    jobs: dict[JobId, JobRecord] = field(default_factory=dict)
    work_items: dict[WorkItemId, WorkItemRow] = field(default_factory=dict)
    artifacts: dict[ArtifactId, ArtifactRecord] = field(default_factory=dict)

    reachable: bool = True
    """What `MemoryDatabaseProbe` reports. A test flips it; nothing else touches it."""


class MemoryDatabaseProbe:
    """`api.ports.DatabaseProbe` over a dictionary.

    It answers the one question `GET /health` asks, and it can answer `False`: an unreachable
    database is a state the health route has to render, and a probe that could only say yes
    would leave that branch untested.
    """

    def __init__(self, database: MemoryDatabase) -> None:
        self._database: MemoryDatabase = database

    async def ping(self) -> bool:
        return self._database.reachable


type SortKey[R] = Callable[[R], tuple[datetime, str]]
"""How a row answers "where am I in this listing": its `(created_at, id)` pair (D072)."""


def keyset_page[R](
    rows: Iterable[R],
    *,
    sort_key: SortKey[R],
    cursor: Cursor | None,
    limit: int,
) -> Page[R]:
    """One page, newest first, positioned by key and never by offset (D072).

    The id breaks ties, so two rows written in the same microsecond still have an order, and the
    next page is "strictly before the last row you saw" rather than "skip N". A row inserted
    while a client pages is therefore never skipped and never repeated: a newer row sorts above
    the cursor and is simply not in the window, and an older one keeps its place.
    """
    ordered = sorted(rows, key=sort_key, reverse=True)
    if cursor is not None:
        position = (cursor.created_at, cursor.id)
        ordered = [row for row in ordered if sort_key(row) < position]
    window = ordered[:limit]
    if not window or len(ordered) == len(window):
        return Page(items=tuple(window), next_cursor=None)
    last_created_at, last_id = sort_key(window[-1])
    return Page(
        items=tuple(window),
        next_cursor=Cursor(created_at=last_created_at, id=last_id).encode(),
    )
