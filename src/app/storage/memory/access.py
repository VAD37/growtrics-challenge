"""`principals`, in memory.

The table exists so every other table has a foreign key to point at (`docs/demo.md`, "Tables").
There is no registration step, so the first request a caller makes is also the row that admits
them, which is why the port is an upsert rather than an insert.
"""

from app.domain.access import Principal
from app.domain.ids import PrincipalId
from app.orchestration.ports import Clock
from app.storage.memory.state import MemoryDatabase

__all__ = ["MemoryPrincipalRepository"]


class MemoryPrincipalRepository:
    """`access.ports.PrincipalRepository` over `MemoryDatabase.principals`."""

    def __init__(self, database: MemoryDatabase, clock: Clock) -> None:
        self._database: MemoryDatabase = database
        self._clock: Clock = clock

    async def upsert(self, *, principal_id: PrincipalId, external_id: str) -> Principal:
        """Return the existing row, or admit this caller.

        `created_at` is the first sighting and is never moved by a later request: it is the only
        record of when a principal appeared, and every request refreshing it would erase that.

        `external_id` on an existing row is left as it was. The demo's resolver passes the
        principal id as the external id (`app/access/stub.py`), so the two cannot legitimately
        disagree, and quietly rewriting the identity a row was created under is not something a
        read path should be able to cause.
        """
        existing = self._database.principals.get(principal_id)
        if existing is not None:
            return existing
        principal = Principal(
            principal_id=principal_id,
            external_id=external_id,
            created_at=self._clock.now(),
        )
        self._database.principals[principal_id] = principal
        return principal
