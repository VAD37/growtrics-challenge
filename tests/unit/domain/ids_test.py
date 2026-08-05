"""Id derivation, encoding, and format guards.

The derivation spine is the scope override that supersedes D055/D089: the backend mints a
`request_key` on every `POST /v1/jobs`, and every other id hangs off it. Every assertion here
is about a value another process has to be able to recompute from that one key.
"""

import re
import uuid
from typing import Final

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import (
    ARTIFACT_ID_PATTERN,
    BRIEF_ID_PATTERN,
    CROCKFORD_ALPHABET,
    ID_BODY_LENGTH,
    ID_SEPARATOR,
    JOB_ID_PATTERN,
    NS_ART,
    NS_BRIEF,
    NS_JOB,
    NS_SESSION,
    NS_TRACE,
    NS_WORK_ITEM,
    REQUEST_KEY_PATTERN,
    SESSION_ID_PATTERN,
    TRACE_ID_PATTERN,
    WORK_ITEM_ID_PATTERN,
    ArtifactId,
    ChatContextId,
    JobId,
    PrincipalId,
    RequestKey,
    decode_crockford,
    derive_artifact_id,
    derive_brief_id,
    derive_job_id,
    derive_session_id,
    derive_trace_id,
    derive_work_item_id,
    encode_crockford,
    is_artifact_id,
    is_brief_id,
    is_chat_context_id,
    is_job_id,
    is_principal_id,
    is_request_key,
    is_session_id,
    is_trace_id,
    is_work_item_id,
    join_id_parts,
    mint_request_key,
    new_request_key,
)

PRINCIPAL: Final[str] = "u_demo"
ENTROPY: Final[bytes] = b"\x11" * 16
REQUEST_KEY: Final[str] = mint_request_key(ENTROPY)
CONTENT_HASH: Final[str] = "sha256:" + "ab" * 32


# --------------------------------------------------------------------------- alphabet


def test_alphabet_is_crockford_base32() -> None:
    assert len(CROCKFORD_ALPHABET) == 32
    assert len(set(CROCKFORD_ALPHABET)) == 32
    assert CROCKFORD_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def test_alphabet_excludes_the_ambiguous_letters() -> None:
    for letter in "ILOU":
        assert letter not in CROCKFORD_ALPHABET


def test_alphabet_matches_the_pattern_used_in_every_minted_id() -> None:
    body = re.compile(r"^[0-9A-HJKMNP-TV-Z]+$")
    assert body.fullmatch(CROCKFORD_ALPHABET) is not None


# --------------------------------------------------------------------------- encoding


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00" * 16,
        b"\xff" * 16,
        b"\x00" * 15 + b"\x01",
        bytes(range(16)),
        uuid.UUID("f50c4698-3f66-55a0-af08-9be8efd7c715").bytes,
    ],
)
def test_crockford_round_trips(raw: bytes) -> None:
    assert decode_crockford(encode_crockford(raw)) == raw


def test_encode_is_fixed_width_and_unpadded() -> None:
    for raw in (b"\x00" * 16, b"\xff" * 16, bytes(range(16))):
        encoded = encode_crockford(raw)
        assert len(encoded) == ID_BODY_LENGTH
        assert "=" not in encoded
        assert set(encoded) <= set(CROCKFORD_ALPHABET)


def test_encode_is_big_endian() -> None:
    assert encode_crockford(b"\x00" * 16) == "0" * 26
    assert encode_crockford(b"\x00" * 15 + b"\x01") == "0" * 25 + "1"


def test_encode_rejects_anything_but_sixteen_bytes() -> None:
    for raw in (b"", b"\x00" * 15, b"\x00" * 17):
        with pytest.raises(DomainError) as caught:
            encode_crockford(raw)
        assert caught.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0" * 25,
        "0" * 27,
        "0" * 25 + "I",  # excluded letter
        "0" * 25 + "L",
        "0" * 25 + "O",
        "0" * 25 + "U",
        "0" * 25 + "a",  # lowercase is not the canonical form
        "0" * 25 + "-",
        "Z" * 26,  # 130 bits of ones does not fit in 16 bytes
    ],
)
def test_decode_rejects_non_canonical_input(text: str) -> None:
    with pytest.raises(DomainError) as caught:
        decode_crockford(text)
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_decode_accepts_the_largest_value_that_fits() -> None:
    largest = encode_crockford(b"\xff" * 16)
    assert decode_crockford(largest) == b"\xff" * 16


# --------------------------------------------------------------------------- namespaces


def test_namespaces_are_distinct() -> None:
    namespaces = [NS_JOB, NS_BRIEF, NS_ART, NS_WORK_ITEM, NS_TRACE, NS_SESSION]
    assert len(set(namespaces)) == len(namespaces)


def test_namespaces_are_pinned_literals() -> None:
    # If one of these ever changes, every id in every database stops being derivable.
    assert uuid.UUID("f50c4698-3f66-55a0-af08-9be8efd7c715") == NS_JOB
    assert uuid.UUID("8d3dddd1-c277-5610-9838-d8358d40a811") == NS_BRIEF
    assert uuid.UUID("29669cfd-e4ba-540d-bd22-a872f24fdbe3") == NS_ART
    assert uuid.UUID("40832641-47e3-56db-b1db-d9f13e81ba22") == NS_WORK_ITEM
    assert uuid.UUID("2e715f24-09ea-5cc5-bd9f-65ec7b994fd8") == NS_TRACE
    assert uuid.UUID("f9b79c27-84d7-588d-aa70-1b740b195490") == NS_SESSION


def test_namespace_literals_match_the_recipe_in_the_comment() -> None:
    root = uuid.uuid5(uuid.NAMESPACE_DNS, "ids.growtrics-challenge.invalid")
    assert uuid.uuid5(root, "job") == NS_JOB
    assert uuid.uuid5(root, "brief") == NS_BRIEF
    assert uuid.uuid5(root, "artifact") == NS_ART
    assert uuid.uuid5(root, "work_item") == NS_WORK_ITEM
    assert uuid.uuid5(root, "trace") == NS_TRACE
    assert uuid.uuid5(root, "session") == NS_SESSION


def test_same_name_under_different_namespaces_gives_different_ids() -> None:
    job_id = derive_job_id(REQUEST_KEY)
    bodies = {
        derive_brief_id(job_id)[len("brf_") :],
        derive_work_item_id(job_id)[len("wi_") :],
    }
    assert len(bodies) == 2


# --------------------------------------------------------------------------- join


def test_join_uses_the_documented_separator_when_no_part_contains_it() -> None:
    assert join_id_parts("a", "b", "c") == f"a{ID_SEPARATOR}b{ID_SEPARATOR}c"


def test_join_is_unambiguous_under_separator_injection() -> None:
    assert join_id_parts("a|b", "c") != join_id_parts("a", "b|c")
    assert join_id_parts("a", "", "b") != join_id_parts("a", "b")
    assert join_id_parts("a\\", "b") != join_id_parts("a", "\\b")


# --------------------------------------------------------------------------- minting


def test_minted_request_key_is_shaped_like_the_contract() -> None:
    assert re.fullmatch(REQUEST_KEY_PATTERN, REQUEST_KEY) is not None
    assert is_request_key(REQUEST_KEY)
    assert REQUEST_KEY.startswith("req_")


def test_minting_is_a_pure_function_of_its_entropy() -> None:
    assert mint_request_key(ENTROPY) == mint_request_key(ENTROPY)
    assert mint_request_key(ENTROPY) != mint_request_key(b"\x12" * 16)


def test_minting_rejects_entropy_that_is_not_sixteen_bytes() -> None:
    for short in (b"", b"\x11" * 8, b"\x11" * 17):
        with pytest.raises(DomainError) as caught:
            mint_request_key(short)
        assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_two_mints_of_a_request_key_differ() -> None:
    # The one non-deterministic input in the system. Two submits of the same body are two jobs.
    keys = {new_request_key() for _ in range(64)}
    assert len(keys) == 64
    assert all(is_request_key(key) for key in keys)


# --------------------------------------------------------------------------- derivation


def test_job_id_is_shaped_like_the_contract() -> None:
    job_id = derive_job_id(REQUEST_KEY)
    assert re.fullmatch(JOB_ID_PATTERN, job_id) is not None
    assert is_job_id(job_id)


def test_same_request_key_gives_the_same_job_id_every_time() -> None:
    assert derive_job_id(REQUEST_KEY) == derive_job_id(REQUEST_KEY)
    assert derive_job_id(REQUEST_KEY) == derive_job_id(mint_request_key(ENTROPY))


def test_different_request_keys_give_different_job_ids() -> None:
    assert derive_job_id(REQUEST_KEY) != derive_job_id(mint_request_key(b"\x12" * 16))


def test_the_whole_chain_hangs_off_one_request_key() -> None:
    request_key = mint_request_key(bytes(range(16)))
    job_id = derive_job_id(request_key)
    brief_id = derive_brief_id(job_id)
    work_item_id = derive_work_item_id(job_id)
    artifact_id = derive_artifact_id(job_id, CONTENT_HASH)

    chain = (request_key, job_id, brief_id, work_item_id, artifact_id)
    assert len(set(chain)) == len(chain)

    assert is_request_key(request_key)
    assert is_job_id(job_id)
    assert is_brief_id(brief_id)
    assert is_work_item_id(work_item_id)
    assert is_artifact_id(artifact_id)


def test_the_chain_is_reproducible_from_the_key_alone() -> None:
    request_key = mint_request_key(bytes(range(16)))
    first = derive_brief_id(derive_job_id(request_key))
    second = derive_brief_id(derive_job_id(request_key))
    assert first == second


def test_derived_ids_are_shaped_like_their_contract() -> None:
    job_id = derive_job_id(REQUEST_KEY)
    assert re.fullmatch(BRIEF_ID_PATTERN, derive_brief_id(job_id)) is not None
    assert re.fullmatch(ARTIFACT_ID_PATTERN, derive_artifact_id(job_id, CONTENT_HASH)) is not None
    assert re.fullmatch(WORK_ITEM_ID_PATTERN, derive_work_item_id(job_id)) is not None
    assert re.fullmatch(TRACE_ID_PATTERN, derive_trace_id(job_id, 0)) is not None
    assert re.fullmatch(SESSION_ID_PATTERN, derive_session_id(job_id, 0)) is not None


def test_every_derived_id_passes_its_own_guard() -> None:
    job_id = derive_job_id(REQUEST_KEY)
    assert is_brief_id(derive_brief_id(job_id))
    assert is_artifact_id(derive_artifact_id(job_id, CONTENT_HASH))
    assert is_work_item_id(derive_work_item_id(job_id))
    assert is_trace_id(derive_trace_id(job_id, 0))
    assert is_session_id(derive_session_id(job_id, 0))


def test_artifact_id_moves_with_the_job_and_with_the_content() -> None:
    job_a = derive_job_id(REQUEST_KEY)
    job_b = derive_job_id(mint_request_key(b"\x22" * 16))
    hash_a = CONTENT_HASH
    hash_b = "sha256:" + "cd" * 32
    assert derive_artifact_id(job_a, hash_a) != derive_artifact_id(job_b, hash_a)
    assert derive_artifact_id(job_a, hash_a) != derive_artifact_id(job_a, hash_b)


def test_artifact_id_resists_separator_injection() -> None:
    # Without escaping, "a|b" + "|" + "c" and "a" + "|" + "b|c" hash the same string.
    job_id = derive_job_id(REQUEST_KEY)
    assert derive_artifact_id(job_id, f"x|{CONTENT_HASH}") != derive_artifact_id(
        f"{job_id}|x", CONTENT_HASH
    )


def test_trace_and_session_move_with_the_attempt() -> None:
    job_id = derive_job_id(REQUEST_KEY)
    assert derive_trace_id(job_id, 0) != derive_trace_id(job_id, 1)
    assert derive_session_id(job_id, 0) != derive_session_id(job_id, 1)


def test_session_id_does_not_contain_the_job_id() -> None:
    # 12-data-control.md: a worker holding a session id cannot address anything else.
    job_id = derive_job_id(REQUEST_KEY)
    assert job_id[len("job_") :] not in derive_session_id(job_id, 0)


# --------------------------------------------------------------------------- guards


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("u_demo", True),
        ("u_" + "a" * 64, True),
        ("u_a.b:c-d_e", True),
        ("u_" + "a" * 65, False),
        ("u_", False),
        ("demo", False),
        ("u_bad space", False),
        ("u_bad/slash", False),
        ("U_demo", False),
    ],
)
def test_principal_id_guard(value: str, expected: bool) -> None:
    assert is_principal_id(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ctx_demo", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("", False),
        ("ctx demo", False),
        ("../etc/passwd", False),
        ("ctx\ndemo", False),
    ],
)
def test_chat_context_id_guard(value: str, expected: bool) -> None:
    assert is_chat_context_id(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("job_" + "0" * 26, True),
        ("job_" + "0" * 25, False),
        ("job_" + "0" * 27, False),
        ("art_" + "0" * 26, False),
        ("job_" + "I" * 26, False),
        ("job_" + "0" * 25 + "a", False),
        ("job_" + "0" * 26 + "\n", False),
    ],
)
def test_job_id_guard(value: str, expected: bool) -> None:
    assert is_job_id(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("req_" + "0" * 26, True),
        ("req_" + "0" * 25, False),
        ("req_" + "0" * 27, False),
        ("job_" + "0" * 26, False),
        ("req_" + "U" * 26, False),
        ("req_" + "0" * 26 + " ", False),
    ],
)
def test_request_key_guard(value: str, expected: bool) -> None:
    assert is_request_key(value) is expected


def test_prefixes_do_not_collide() -> None:
    body = "0" * 26
    assert not is_job_id(f"art_{body}")
    assert not is_artifact_id(f"job_{body}")
    assert not is_trace_id(f"brf_{body}")
    assert not is_work_item_id(f"tr_{body}")
    assert not is_request_key(f"job_{body}")


# --------------------------------------------------------------------------- pydantic edge


def test_annotated_aliases_validate_at_a_trust_boundary() -> None:
    job_adapter = TypeAdapter(JobId)
    job_id = derive_job_id(REQUEST_KEY)
    assert job_adapter.validate_python(job_id) == job_id
    with pytest.raises(ValidationError):
        job_adapter.validate_python("job_not-a-real-id")


@pytest.mark.parametrize(
    ("alias", "good", "bad"),
    [
        (ArtifactId, "art_" + "0" * 26, "art_" + "0" * 25),
        (PrincipalId, "u_demo", "demo"),
        (ChatContextId, "ctx_demo", "ctx demo"),
        (RequestKey, "req_" + "0" * 26, "req_" + "0" * 25),
    ],
)
def test_each_alias_rejects_its_own_bad_value(alias: object, good: str, bad: str) -> None:
    # @audit `object` here is the PEP 695 alias itself, which has no useful static type.
    adapter: TypeAdapter[str] = TypeAdapter(alias)
    assert adapter.validate_python(good) == good
    with pytest.raises(ValidationError):
        adapter.validate_python(bad)
