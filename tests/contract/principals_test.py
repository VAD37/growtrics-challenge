"""`PrincipalRepository`, against both backends.

The table exists so every other table has a foreign key to point at, and there is no registration
step: the first request a caller makes is also the row that admits them. That makes the port an
upsert, and it makes the thing worth asserting the one an upsert gets wrong -- a second sighting
must not mint a second row and must not move the first one's `created_at`, which is the only
record of when a principal appeared.
"""

from support.backends import StorageBackend
from support.rows import OTHER_PRINCIPAL, PRINCIPAL, T0


async def test_the_first_request_a_caller_makes_admits_them(backend: StorageBackend) -> None:
    principal = await backend.principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)

    assert principal.principal_id == PRINCIPAL
    assert principal.external_id == PRINCIPAL
    assert principal.created_at == T0


async def test_a_second_sighting_keeps_the_row_and_its_created_at(
    backend: StorageBackend,
) -> None:
    """Every request upserts. One that refreshed `created_at` would erase the first sighting."""
    first = await backend.principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)
    backend.clock.advance(3600)

    again = await backend.principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)

    assert again == first
    assert again.created_at == T0


async def test_a_second_sighting_does_not_rewrite_the_identity_the_row_was_created_under(
    backend: StorageBackend,
) -> None:
    """A read path that could rewrite `external_id` could rename a caller by asking about them."""
    first = await backend.principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)

    again = await backend.principals.upsert(principal_id=PRINCIPAL, external_id="somebody-else")

    assert again.external_id == first.external_id


async def test_two_callers_get_two_rows(backend: StorageBackend) -> None:
    mine = await backend.principals.upsert(principal_id=PRINCIPAL, external_id=PRINCIPAL)
    theirs = await backend.principals.upsert(
        principal_id=OTHER_PRINCIPAL, external_id=OTHER_PRINCIPAL
    )

    assert mine.principal_id != theirs.principal_id
    assert mine.created_at == theirs.created_at == T0
