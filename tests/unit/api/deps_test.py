"""Injection points: the providers, the scope, and the page parameters.

Everything behind a router arrives through `deps.py`, and every provider is a stub that
`main.py` overrides. That is what lets this lane be finished, tested, and merged before
orchestration, custody or storage exist.
"""

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Final

import pytest
from api_fakes import FakeJobService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import install
from app.api.deps import (
    USER_ID_HEADER,
    PageParams,
    get_artifact_service,
    get_database_probe,
    get_job_service,
    get_principal_resolver,
    get_settings,
    page_params,
)
from app.config import Settings, settings
from app.domain.errors import DomainError, ErrorCode
from app.domain.records import Cursor

NOW: Final[datetime] = datetime(2026, 8, 5, 9, 12, 3, tzinfo=UTC)
INSTRUCTION: Final[str] = "why do atoms form covalent bonds"

# --------------------------------------------------------------------------- providers


@pytest.mark.parametrize(
    "provider",
    [get_job_service, get_artifact_service, get_database_probe, get_principal_resolver],
)
def test_every_port_provider_is_a_stub_until_main_overrides_it(
    provider: Callable[[], object],
) -> None:
    # One test for all four: the bodies are `raise NotImplementedError` under a @TODO, and the
    # composition root replaces them with adapters. A router that runs without wiring fails
    # loudly rather than serving something invented.
    with pytest.raises(NotImplementedError):
        provider()


def test_settings_are_the_one_reader_of_the_environment() -> None:
    assert isinstance(get_settings(), Settings)


def test_the_identity_header_is_the_documented_one() -> None:
    assert USER_ID_HEADER == "X-User-Id"


# --------------------------------------------------------------------------- paging


def test_the_default_page_has_no_cursor() -> None:
    assert page_params(cursor=None, limit=20) == PageParams(cursor=None, limit=20)


def test_a_cursor_is_decoded_once_at_the_edge() -> None:
    cursor = Cursor(created_at=NOW, id="job_" + "0" * 26)
    assert page_params(cursor=cursor.encode(), limit=20).cursor == cursor


def test_a_malformed_cursor_is_an_invalid_request() -> None:
    with pytest.raises(DomainError) as caught:
        page_params(cursor="!!!!", limit=20)
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert caught.value.details["field"] == "cursor"


def test_the_page_params_record_is_frozen() -> None:
    params = page_params(cursor=None, limit=5)
    with pytest.raises(FrozenInstanceError):
        params.limit = 6  # type: ignore[misc]


def test_the_configured_bounds_are_the_ones_the_routers_use() -> None:
    assert settings.page_default_limit <= settings.page_max_limit


# --------------------------------------------------------------------------- scope


def test_a_request_with_no_credential_is_served_anyway(client: TestClient) -> None:
    # @audit no authentication. A request with no header is not refused; it becomes the
    # default principal and reads whatever it asks for.
    response = client.post("/v1/jobs", json={"instruction": INSTRUCTION})
    assert response.status_code == 202


def test_the_default_principal_is_the_configured_one(
    client: TestClient, job_service: FakeJobService
) -> None:
    client.post("/v1/jobs", json={"instruction": INSTRUCTION})
    scope, _, _ = job_service.submitted[0]
    assert scope.principal_id == settings.default_principal_id


def test_the_header_names_the_caller(client: TestClient, job_service: FakeJobService) -> None:
    client.post("/v1/jobs", json={"instruction": INSTRUCTION}, headers={USER_ID_HEADER: "u_alice"})
    scope, _, _ = job_service.submitted[0]
    assert scope.principal_id == "u_alice"


def test_the_scope_handed_to_a_service_enforces_no_ownership(
    client: TestClient, job_service: FakeJobService
) -> None:
    # @audit the flag is the missing authorisation, carried as a value a test can assert on.
    client.post("/v1/jobs", json={"instruction": INSTRUCTION})
    scope, _, _ = job_service.submitted[0]
    assert scope.ownership_enforced is False


def test_an_unwired_app_raises_rather_than_answering() -> None:
    # The same routers with no dependency overrides: the stub providers fire.
    bare = FastAPI()
    install(bare)
    client = TestClient(bare, raise_server_exceptions=True)
    with pytest.raises(NotImplementedError):
        client.get("/health")
