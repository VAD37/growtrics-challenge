"""What the edge needs from `access`, as shapes rather than as classes (D086).

Two ports. `PrincipalResolver` turns whatever a request carried into a `Principal` and then
into an `AccessScope`; `PrincipalRepository` is the `principals` table, owned by the storage
lane. Declaring the resolver as a protocol is what makes "swapping in a real bearer token
changes one function" true: a token resolver is a second implementation of this shape, and no
router, no dependency, and no repository signature moves when it arrives.

@audit the demo implementation of `PrincipalResolver` authenticates nothing. See
`app/access/stub.py`, which is the module the audit line belongs to.
"""

from typing import Protocol, runtime_checkable

from app.domain.access import AccessScope, Principal
from app.domain.ids import PrincipalId

__all__ = ["PrincipalRepository", "PrincipalResolver"]


@runtime_checkable
class PrincipalRepository(Protocol):
    """The `principals` table, which exists so every other table has a foreign key.

    Upsert rather than insert: the demo has no registration step, so the first request a
    caller makes is also the row that admits them. Implemented by the storage lane.
    """

    async def upsert(self, *, principal_id: PrincipalId, external_id: str) -> Principal: ...


@runtime_checkable
class PrincipalResolver(Protocol):
    """Steps 1 and 2 of `plan/12-data-control.md`: who is asking, and what may they reach.

    `resolve` takes the raw claim off the wire -- `None` when the header was absent -- because
    deciding what an absent or malformed credential means is exactly the decision this port
    exists to make swappable. `scope_for` is the only way anything obtains an `AccessScope`
    (D067).
    """

    async def resolve(self, claimed_id: str | None) -> Principal: ...

    def scope_for(self, principal: Principal) -> AccessScope: ...
