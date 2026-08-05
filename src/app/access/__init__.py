"""STUB. Principal resolution and `AccessScope` minting (D067, D086).

@audit no authentication and no authorisation. `app/access/stub.py` believes the `X-User-Id`
header, falls back to the configured default when it is absent, and mints a scope that
enforces no ownership. Entitlement and balance holds are not built at all: the demo cut cost
metering (`docs/demo.md`), so there is nothing to hold a balance against.
"""

from app.access.ports import PrincipalRepository, PrincipalResolver
from app.access.stub import DemoPrincipalResolver, normalise_principal_id, scope_for

__all__ = [
    "DemoPrincipalResolver",
    "PrincipalRepository",
    "PrincipalResolver",
    "normalise_principal_id",
    "scope_for",
]
