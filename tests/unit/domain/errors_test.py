"""The error catalog is the only place a client-facing message is written (D062)."""

from enum import StrEnum

import pytest

from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode, ErrorEntry

# docs/demo.md, "Errors". Transcribed rather than imported, so a change to the module has to
# be a deliberate change to this table too.
DEMO_TABLE: dict[str, int | None] = {
    "INVALID_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "JOB_NOT_FOUND": 404,
    "ARTIFACT_NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "ARTIFACT_NOT_READY": 409,
    "TOO_MANY_ACTIVE_JOBS": 429,
    "GENERATION_FAILED": None,
}


# --------------------------------------------------------------------------- the enum


def test_error_code_is_a_string_enum() -> None:
    assert issubclass(ErrorCode, StrEnum)


def test_error_code_values_equal_their_names() -> None:
    # The wire value is the member name. D072: enums are text, in the database and in JSON.
    for code in ErrorCode:
        assert code.value == code.name


def test_the_demo_needs_exactly_these_eight_codes() -> None:
    assert {code.value for code in ErrorCode} == set(DEMO_TABLE)


# --------------------------------------------------------------------------- the catalog


def test_catalog_is_total_over_the_enum() -> None:
    assert set(ERROR_CATALOG) == set(ErrorCode)


def test_catalog_http_statuses_match_the_demo_table() -> None:
    for code in ErrorCode:
        assert ERROR_CATALOG[code].http_status == DEMO_TABLE[code.value]


def test_generation_failed_is_a_job_failure_and_not_an_http_status() -> None:
    assert ERROR_CATALOG[ErrorCode.GENERATION_FAILED].http_status is None


def test_every_other_code_carries_a_client_facing_status() -> None:
    for code, entry in ERROR_CATALOG.items():
        if code is ErrorCode.GENERATION_FAILED:
            continue
        assert entry.http_status is not None
        assert 400 <= entry.http_status < 500


def test_messages_are_present_and_tidy() -> None:
    for entry in ERROR_CATALOG.values():
        assert entry.message
        assert entry.message == entry.message.strip()


@pytest.mark.parametrize("marker", ["{", "}", "%s", "%d", "$", "format(", "f'", 'f"'])
def test_no_catalog_message_can_interpolate_caller_text(marker: str) -> None:
    # The leak check. A message with a placeholder is a message somebody will fill from a
    # request, and then the catalog stops being the one place to audit.
    for code, entry in ERROR_CATALOG.items():
        assert marker not in entry.message, code


def test_messages_are_distinct_per_code() -> None:
    messages = [entry.message for entry in ERROR_CATALOG.values()]
    assert len(set(messages)) == len(messages)


def test_catalog_entries_are_frozen() -> None:
    entry = ERROR_CATALOG[ErrorCode.JOB_NOT_FOUND]
    with pytest.raises(AttributeError):
        entry.http_status = 200  # type: ignore[misc]


def test_catalog_itself_is_read_only() -> None:
    with pytest.raises(TypeError):
        ERROR_CATALOG[ErrorCode.JOB_NOT_FOUND] = ErrorEntry(  # type: ignore[index]
            http_status=200, message="nope"
        )


# --------------------------------------------------------------------------- the exception


def test_domain_error_carries_a_code_and_nothing_written_at_the_raise_site() -> None:
    error = DomainError(ErrorCode.JOB_NOT_FOUND)
    assert error.code is ErrorCode.JOB_NOT_FOUND
    assert error.details == {}
    assert not hasattr(error, "message")


def test_domain_error_stringifies_to_its_code() -> None:
    assert str(DomainError(ErrorCode.TOO_MANY_ACTIVE_JOBS)) == "TOO_MANY_ACTIVE_JOBS"


def test_domain_error_details_are_copied_and_immutable() -> None:
    supplied = {"field": "max_duration_s"}
    error = DomainError(ErrorCode.INVALID_REQUEST, supplied)
    supplied["field"] = "mutated"
    assert error.details["field"] == "max_duration_s"
    with pytest.raises(TypeError):
        error.details["field"] = "also mutated"  # type: ignore[index]


def test_domain_error_is_catchable_as_an_exception() -> None:
    with pytest.raises(DomainError) as caught:
        raise DomainError(ErrorCode.ARTIFACT_NOT_READY)
    assert caught.value.code is ErrorCode.ARTIFACT_NOT_READY
