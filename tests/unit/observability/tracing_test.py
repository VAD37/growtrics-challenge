"""Trace id creation is real; propagation is a stub.

Minting is three lines off `app.domain.ids` and worth testing properly, because a trace id that
does not match `TRACE_ID_PATTERN` is a column value the eventual `job_events.trace_id` rejects.
Propagation is one stub test and no more.
"""

from typing import Final

import pytest

from app.domain.errors import DomainError
from app.domain.ids import derive_trace_id, is_trace_id
from app.observability.tracing import (
    TRACE_HEADER,
    TRACE_PREFIX,
    bind_trace_id,
    current_trace_id,
    mint_trace_id,
    new_trace_id,
    trace_id_for_attempt,
)

JOB_ID: Final[str] = "job_0123456789ABCDEFGHJKMNPQRS"
ENTROPY: Final[bytes] = bytes(range(16))


# --------------------------------------------------------------------------- minting


def test_a_minted_trace_id_is_a_trace_id() -> None:
    assert is_trace_id(new_trace_id())


def test_minting_is_pure_in_its_entropy() -> None:
    # The random source sits in `new_trace_id` alone, the same split `mint_request_key` uses,
    # so a test can pin a trace id without patching a module global.
    assert mint_trace_id(ENTROPY) == mint_trace_id(ENTROPY)


def test_two_mints_differ() -> None:
    assert new_trace_id() != new_trace_id()


def test_the_prefix_is_the_one_the_id_pattern_expects() -> None:
    assert new_trace_id().startswith(TRACE_PREFIX)


def test_wrong_entropy_width_is_rejected() -> None:
    # `encode_crockford` owns this rule; the test is here so shortening the argument silently
    # produces an error rather than a truncated id.
    with pytest.raises(DomainError):
        mint_trace_id(b"\x00" * 8)


# --------------------------------------------------------------------------- per attempt


def test_the_per_attempt_id_is_the_domain_derivation() -> None:
    assert trace_id_for_attempt(JOB_ID, 0) == derive_trace_id(JOB_ID, 0)


def test_a_retry_gets_its_own_trace() -> None:
    assert trace_id_for_attempt(JOB_ID, 0) != trace_id_for_attempt(JOB_ID, 1)


# --------------------------------------------------------------------------- propagation


def test_the_header_name_is_the_one_the_edge_echoes() -> None:
    assert TRACE_HEADER == "X-Trace-Id"


def test_propagation_is_a_stub() -> None:
    # One stub test for the whole deferred half. Until it is filled in, a trace id is passed as
    # an argument or it is not passed at all.
    with pytest.raises(NotImplementedError):
        bind_trace_id(new_trace_id())
    with pytest.raises(NotImplementedError):
        current_trace_id()
