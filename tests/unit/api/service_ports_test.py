"""The seam: `JobService`, `ArtifactService`, `DatabaseProbe`.

Structural typing on purpose. The protocols live in `app/api/ports.py` so the API depends on a
shape rather than on `app.orchestration`, and orchestration satisfies them without importing
`app.api` -- which is what keeps the import-linter contract "api reaches no adapter" true while
both sides agree on one signature.

One conformance test per port, plus the argument list itself, because a keyword renamed on one
side of a structural seam fails at call time rather than at import time.
"""

import inspect
from collections.abc import Mapping

from api_fakes import FakeArtifactService, FakeDatabaseProbe, FakeJobService

from app.api.ports import ArtifactService, DatabaseProbe, JobService


def test_the_job_fake_satisfies_the_port() -> None:
    assert isinstance(FakeJobService(), JobService)


def test_the_artifact_fake_satisfies_the_port() -> None:
    assert isinstance(FakeArtifactService(), ArtifactService)


def test_the_probe_fake_satisfies_the_port() -> None:
    assert isinstance(FakeDatabaseProbe(), DatabaseProbe)


def test_submit_takes_the_scope_the_command_and_the_raw_body() -> None:
    # Scope override item 4: the raw body travels with the command so orchestration can write
    # the `requests` row without the API touching a table.
    signature = inspect.signature(JobService.submit)
    assert list(signature.parameters) == ["self", "scope", "command", "raw_body"]
    assert signature.parameters["raw_body"].annotation == Mapping[str, object]


def test_every_read_takes_a_scope_first() -> None:
    # D067. An unscoped query is a missing argument rather than a review finding.
    reads = (JobService.get, JobService.list, ArtifactService.list, ArtifactService.open_content)
    for method in reads:
        assert list(inspect.signature(method).parameters)[:2] == ["self", "scope"], method


def test_paging_arguments_are_keyword_only() -> None:
    for method in (JobService.list, ArtifactService.list):
        parameters = inspect.signature(method).parameters
        assert parameters["cursor"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_artifact_listing_filter_is_optional_and_keyword_only() -> None:
    parameter = inspect.signature(ArtifactService.list).parameters["job_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_ports_are_the_whole_seam() -> None:
    # Nothing else crosses. If the edge needs a third question answered, it becomes a method
    # on one of these rather than an import of another package.
    from app.api import ports

    assert set(ports.__all__) == {"ArtifactService", "DatabaseProbe", "JobService"}
