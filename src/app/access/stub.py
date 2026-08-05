"""The demo's identity, such as it is.

@audit no authorisation. `X-User-Id` is an unauthenticated claim and no read is scoped to it.
Any caller reads any job and any artifact. This is a free demo API with no real data in it.
Restoring the check: give `AccessScope` a principal predicate, re-add the `WHERE` clause in
repositories, and turn the 200 below into `JOB_NOT_FOUND`.

Concretely, this module never raises `UNAUTHENTICATED`. A request with no header becomes
`settings.default_principal_id`, a request with a malformed header becomes the same, and a
request naming any other user is believed. The resolver is declared as a port (D086) so a
bearer-token implementation is a second class here rather than an edit spread through the
routers, and every scope it mints carries `ownership_enforced=False` so the absence is a value
a test asserts on rather than a comment somebody has to notice.

Supersedes D060 and D068 for the demo, see the scope override.
"""

import re
from typing import Final

from app.access.ports import PrincipalRepository
from app.domain.access import AccessScope, Principal
from app.domain.ids import PRINCIPAL_ID_PATTERN, PrincipalId

_PRINCIPAL_ID_RE: Final[re.Pattern[str]] = re.compile(PRINCIPAL_ID_PATTERN)


def normalise_principal_id(claimed: str | None, fallback: PrincipalId) -> PrincipalId:
    """Turn a header value into a principal id, or fall back to the demo default.

    The pattern is the one `domain/ids.py` owns, applied here rather than at the edge, because
    "what counts as an identity" is this module's question. A value that does not match is not
    an error: there is nothing to authenticate, so an unusable claim is treated as no claim.
    Rejecting it also keeps `\\r\\n` and `/` out of a value that later reaches log lines and
    SQL parameters.
    """
    if claimed is None:
        return fallback
    candidate = claimed.strip()
    if _PRINCIPAL_ID_RE.fullmatch(candidate) is None:
        return fallback
    return candidate


def scope_for(principal: Principal) -> AccessScope:
    """The only way anything in this system obtains an `AccessScope` (D067).

    @audit the scope is permissive. `ownership_enforced=False` tells every repository to run
    its predicate-free statement, so the scope names a caller without limiting them to their
    own rows. Restoring the check is `ownership_enforced=True` here and the `WHERE` clause in
    `app/storage/sql/repositories.py`.
    """
    return AccessScope._mint(principal.principal_id)


class DemoPrincipalResolver:
    """`PrincipalResolver` for a free demo API. Believes what it is told.

    Upserts through the repository on every resolve so that `jobs.principal_id` and
    `artifacts.principal_id` always have a row to point at. That is the one real job this
    class does; the rest of it is an apology.
    """

    def __init__(
        self, repository: PrincipalRepository, *, default_principal_id: PrincipalId
    ) -> None:
        self._repository: PrincipalRepository = repository
        self._default_principal_id: PrincipalId = default_principal_id

    async def resolve(self, claimed_id: str | None) -> Principal:
        """@audit no verification of any kind. The claim is the identity."""
        principal_id = normalise_principal_id(claimed_id, self._default_principal_id)
        return await self._repository.upsert(principal_id=principal_id, external_id=principal_id)

    def scope_for(self, principal: Principal) -> AccessScope:
        return scope_for(principal)
