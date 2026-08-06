"""`briefs`, in memory. One write, one read, and no way to change a sealed row.

Intake is the sole writer of this table (D066) and it writes each row exactly once: a brief is
the sealed intermediary product, and `brief_hash` is only worth recording while the row it
describes cannot be edited afterwards. So there is no update method here to edit one with, and a
second insert under the same id is refused rather than merged -- `brief_id` is
`uuid5(NS_BRIEF, job_id)`, so a collision means the same job was sealed twice and one of the two
briefs is about to be forgotten.

@TODO no port declares this shape yet. `orchestration.ports.BriefWriter` is intake's service
seen from outside -- it sanitises, seals and inserts -- and the seam between that service and
this table lands with the intake lane. Both backends already have the method it will name.
"""

from app.domain.ids import BriefId
from app.domain.records import BriefRecord
from app.storage.errors import IntegrityError
from app.storage.memory.state import MemoryDatabase

__all__ = ["MemoryBriefRepository"]


class MemoryBriefRepository:
    """The `briefs` table over `MemoryDatabase.briefs`."""

    def __init__(self, database: MemoryDatabase) -> None:
        self._database: MemoryDatabase = database

    async def insert(self, record: BriefRecord) -> BriefRecord:
        """Write a sealed brief. A second one under the same id is a bug, not an update."""
        if record.brief_id in self._database.briefs:
            raise IntegrityError(f"brief {record.brief_id} already exists")
        self._database.briefs[record.brief_id] = record
        return record

    async def load(self, brief_id: BriefId) -> BriefRecord | None:
        """The sealed brief a run is working from. No scope: the runner reads it, not a caller."""
        return self._database.briefs.get(brief_id)
