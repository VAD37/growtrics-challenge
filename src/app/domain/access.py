"""Who is asking, and what that is allowed to reach.

@audit NO AUTHORISATION. This is a free demo API and the check is deliberately absent. Any
caller reads any job and any artifact: the scope minted at the edge carries a principal id and
`ownership_enforced=False`, and no repository read applies an owner predicate. Restoring the
check means minting the scope with `ownership_enforced=True` and re-adding the `WHERE
principal_id = :principal_id` clause in `app/storage/sql/repositories.py`, where both the
predicate-free and the predicate-carrying statements already sit side by side.

Supersedes D060 and D068 for the demo, see the scope override. D067 survives intact and is the
reason this is cheap to undo: every repository read still takes an `AccessScope` first
argument, so switching the demo back to real authorisation fills in bodies rather than changing
signatures. An unscoped query stays a missing argument rather than a review finding.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import PrincipalId


@dataclass(frozen=True, slots=True)
class Principal:
    """A resolved caller. Upserted on first sight of an `X-User-Id` header (D086).

    `external_id` is what the caller presented; `principal_id` is what this system calls them.
    Keeping them separate is what lets the header be swapped for a bearer subject later without
    rewriting every foreign key.
    """

    principal_id: PrincipalId
    external_id: str
    created_at: datetime


_MINT_TOKEN: Final[object] = object()
"""Module-private. Holding it is the proof that `access` built this scope, not a router."""


@dataclass(frozen=True, slots=True)
class AccessScope:
    """The permission to read, passed as the first argument of every repository read (D067).

    `ownership_enforced` is the whole authorisation model, and in the demo it is `False`. A
    repository handed such a scope runs its predicate-free statement and returns any row that
    matches the query, whoever owns it. The flag exists so that the absence is a value a test
    can assert on rather than a comment somebody has to notice.
    """

    principal_id: PrincipalId
    ownership_enforced: bool
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _MINT_TOKEN:
            raise DomainError(ErrorCode.UNAUTHENTICATED, {"reason": "scope was not minted"})

    @classmethod
    def _mint(cls, principal_id: PrincipalId, *, ownership_enforced: bool = False) -> AccessScope:
        """The only constructor. Called by `app.access` and by nothing else (D067).

        Private by name because the guarantee it carries is "somebody checked", and a caller
        that reaches past the underscore to build its own scope is asserting a check it did not
        run. The demo's default is the permissive scope this whole module is an apology for.
        """
        return cls(
            principal_id=principal_id,
            ownership_enforced=ownership_enforced,
            _token=_MINT_TOKEN,
        )
