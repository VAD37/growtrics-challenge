"""The auth stub, tested for the behaviour the demo actually has.

@audit these assertions describe an API with no authentication and no authorisation. A caller
names itself in `X-User-Id`, nothing verifies the claim, and a caller that names nobody becomes
the default principal instead of being refused. The tests are written that way on purpose: the
absent check is a property somebody has to delete a test to restore, not a comment that rots.
"""

from datetime import UTC, datetime
from typing import Final

import pytest

from app.access.ports import PrincipalRepository, PrincipalResolver
from app.access.stub import DemoPrincipalResolver, normalise_principal_id, scope_for
from app.domain.access import AccessScope, Principal
from app.domain.ids import PrincipalId

DEFAULT_PRINCIPAL: Final[str] = "u_demo"
CREATED_AT: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)


class FakePrincipalRepository:
    """In-memory `principals`. Upsert is what gives every other table its foreign key."""

    def __init__(self) -> None:
        self.rows: dict[str, Principal] = {}
        self.calls: list[tuple[str, str]] = []

    async def upsert(self, *, principal_id: PrincipalId, external_id: str) -> Principal:
        self.calls.append((principal_id, external_id))
        existing = self.rows.get(principal_id)
        if existing is not None:
            return existing
        row = Principal(principal_id=principal_id, external_id=external_id, created_at=CREATED_AT)
        self.rows[principal_id] = row
        return row


def build_resolver() -> tuple[DemoPrincipalResolver, FakePrincipalRepository]:
    repository = FakePrincipalRepository()
    resolver = DemoPrincipalResolver(repository, default_principal_id=DEFAULT_PRINCIPAL)
    return resolver, repository


# --------------------------------------------------------------------------- conformance


def test_the_fake_repository_satisfies_the_port() -> None:
    assert isinstance(FakePrincipalRepository(), PrincipalRepository)


def test_the_stub_satisfies_the_resolver_port() -> None:
    resolver, _ = build_resolver()
    assert isinstance(resolver, PrincipalResolver)


# --------------------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    "claimed",
    ["u_demo", "u_alice", "u_bob", "u_a.b:c-d", "u_" + "x" * 64],
)
def test_a_well_formed_claim_is_taken_at_face_value(claimed: str) -> None:
    # @audit "taken at face value" is the whole authentication story. Nothing verifies it.
    assert normalise_principal_id(claimed, DEFAULT_PRINCIPAL) == claimed


@pytest.mark.parametrize(
    "claimed",
    [
        None,  # header absent
        "",  # header present and empty
        "   ",  # whitespace only
        "alice",  # no prefix
        "u_",  # prefix and nothing else
        "u_bad/slash",  # path separator
        "u_bad space",
        "u_" + "x" * 65,  # one over the pattern's bound
        "u_\r\nX-Injected: 1",  # header injection attempt
    ],
)
def test_a_claim_that_is_not_a_principal_id_falls_back_to_the_default(claimed: str | None) -> None:
    assert normalise_principal_id(claimed, DEFAULT_PRINCIPAL) == DEFAULT_PRINCIPAL


def test_surrounding_whitespace_is_stripped_before_the_pattern_is_applied() -> None:
    assert normalise_principal_id("  u_alice  ", DEFAULT_PRINCIPAL) == "u_alice"


# --------------------------------------------------------------------------- resolution


async def test_an_absent_header_resolves_to_the_default_principal() -> None:
    resolver, _ = build_resolver()
    principal = await resolver.resolve(None)
    assert principal.principal_id == DEFAULT_PRINCIPAL


async def test_resolution_never_raises_unauthenticated() -> None:
    # @audit `UNAUTHENTICATED` is in ERROR_CATALOG and this stub cannot raise it. There is no
    # credential to be missing. Restoring the check is where that code comes back into use.
    resolver, _ = build_resolver()
    for claimed in (None, "", "not-a-principal", "u_alice"):
        assert await resolver.resolve(claimed) is not None


async def test_resolution_upserts_the_principal_so_foreign_keys_hold() -> None:
    resolver, repository = build_resolver()
    await resolver.resolve("u_alice")
    assert repository.calls == [("u_alice", "u_alice")]
    assert "u_alice" in repository.rows


async def test_a_rejected_claim_is_not_recorded_as_an_external_id() -> None:
    # A malformed header is not a caller we know about, so nothing of theirs is stored.
    resolver, repository = build_resolver()
    await resolver.resolve("alice")
    assert repository.calls == [(DEFAULT_PRINCIPAL, DEFAULT_PRINCIPAL)]


async def test_resolving_twice_upserts_rather_than_duplicating() -> None:
    resolver, repository = build_resolver()
    first = await resolver.resolve("u_alice")
    second = await resolver.resolve("u_alice")
    assert first == second
    assert len(repository.rows) == 1


# --------------------------------------------------------------------------- scope


async def test_the_scope_carries_the_resolved_principal() -> None:
    resolver, _ = build_resolver()
    principal = await resolver.resolve("u_alice")
    assert resolver.scope_for(principal).principal_id == "u_alice"


def test_the_minted_scope_enforces_no_ownership() -> None:
    # @audit this is the missing authorisation, asserted. Every repository read handed this
    # scope runs its predicate-free statement, so any caller reads any row.
    principal = Principal(
        principal_id=DEFAULT_PRINCIPAL, external_id=DEFAULT_PRINCIPAL, created_at=CREATED_AT
    )
    assert scope_for(principal).ownership_enforced is False


def test_the_scope_is_a_domain_access_scope() -> None:
    principal = Principal(
        principal_id=DEFAULT_PRINCIPAL, external_id=DEFAULT_PRINCIPAL, created_at=CREATED_AT
    )
    assert isinstance(scope_for(principal), AccessScope)
