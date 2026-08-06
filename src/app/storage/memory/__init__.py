"""Test doubles only, kept honest by the same contract suite as the SQL adapters (D049).

One double per port, all sharing one `MemoryDatabase`, so a job can be submitted through the
unit of work, claimed off the queue, moved by the repository and read back the way it would be
against Postgres. They are not sketches: the transaction is atomic, the queue is FIFO by
`available_at`, the job repository refuses a lost update, and paging is a keyset cursor rather
than an offset.

What they are not is a second implementation of the schema. They hold no SQL, no migration and
no constraint the database does not also have; where one of them enforces something -- a
duplicate key, a version match, the `LEARNER`/`CLEAN` listing predicate -- it is enforcing what
`plan/13-mvp.md` already froze into an index or a constraint.
"""

from app.storage.errors import (
    IntegrityError,
    RowNotFoundError,
    StorageError,
    VersionConflictError,
)
from app.storage.memory.access import MemoryPrincipalRepository
from app.storage.memory.artifacts import MemoryArtifactRepository
from app.storage.memory.briefs import MemoryBriefRepository
from app.storage.memory.clock import FrozenClock
from app.storage.memory.jobs import (
    CommitPoint,
    MemoryJobRepository,
    MemoryRequestStore,
    MemoryUnitOfWork,
)
from app.storage.memory.objects import MEMORY_SCHEME, MemoryObjectStore
from app.storage.memory.queue import MemoryWorkQueue
from app.storage.memory.state import (
    MemoryDatabase,
    MemoryDatabaseProbe,
    WorkItemRow,
)

__all__ = [
    "MEMORY_SCHEME",
    "CommitPoint",
    "FrozenClock",
    "IntegrityError",
    "MemoryArtifactRepository",
    "MemoryBriefRepository",
    "MemoryDatabase",
    "MemoryDatabaseProbe",
    "MemoryJobRepository",
    "MemoryObjectStore",
    "MemoryPrincipalRepository",
    "MemoryRequestStore",
    "MemoryUnitOfWork",
    "MemoryWorkQueue",
    "RowNotFoundError",
    "StorageError",
    "VersionConflictError",
    "WorkItemRow",
]
