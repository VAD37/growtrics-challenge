"""Principal and scope.

The demo scope is permissive by decision, so most of what is asserted here is that the shape
which would carry a real check exists and is honest about being switched off.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Final

import pytest

from app.domain.access import AccessScope, Principal
from app.domain.errors import DomainError, ErrorCode

PRINCIPAL_ID: Final[str] = "u_demo"
CREATED_AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)


# --------------------------------------------------------------------------- principal


def test_principal_is_a_frozen_value() -> None:
    principal = Principal(
        principal_id=PRINCIPAL_ID, external_id="demo@example.invalid", created_at=CREATED_AT
    )
    with pytest.raises(FrozenInstanceError):
        principal.principal_id = "u_other"  # type: ignore[misc]


def test_principal_equality_is_by_value() -> None:
    first = Principal(principal_id=PRINCIPAL_ID, external_id="x", created_at=CREATED_AT)
    second = Principal(principal_id=PRINCIPAL_ID, external_id="x", created_at=CREATED_AT)
    assert first == second


def test_principal_has_no_dict() -> None:
    principal = Principal(principal_id=PRINCIPAL_ID, external_id="x", created_at=CREATED_AT)
    assert not hasattr(principal, "__dict__")


# --------------------------------------------------------------------------- scope


def test_scope_cannot_be_built_by_calling_the_constructor() -> None:
    # D067: the only way to obtain a scope is through the access module's minting call.
    with pytest.raises(DomainError) as caught:
        AccessScope(principal_id=PRINCIPAL_ID, ownership_enforced=False)
    assert caught.value.code is ErrorCode.UNAUTHENTICATED


def test_minted_scope_carries_the_principal() -> None:
    scope = AccessScope._mint(PRINCIPAL_ID)
    assert scope.principal_id == PRINCIPAL_ID


def test_the_demo_scope_enforces_no_ownership() -> None:
    # @audit this assertion is the missing authorisation, written down. Any caller reads any row.
    scope = AccessScope._mint(PRINCIPAL_ID)
    assert scope.ownership_enforced is False


def test_a_scope_can_be_minted_with_ownership_enforced() -> None:
    # Restoring the check is flipping this flag and filling in the repository predicate.
    scope = AccessScope._mint(PRINCIPAL_ID, ownership_enforced=True)
    assert scope.ownership_enforced is True


def test_scope_is_frozen() -> None:
    scope = AccessScope._mint(PRINCIPAL_ID)
    with pytest.raises(FrozenInstanceError):
        scope.ownership_enforced = True  # type: ignore[misc]


def test_scope_equality_ignores_the_mint_token() -> None:
    assert AccessScope._mint(PRINCIPAL_ID) == AccessScope._mint(PRINCIPAL_ID)
    assert AccessScope._mint(PRINCIPAL_ID) != AccessScope._mint("u_other")


def test_scope_repr_does_not_leak_the_mint_token() -> None:
    assert "token" not in repr(AccessScope._mint(PRINCIPAL_ID)).lower()
