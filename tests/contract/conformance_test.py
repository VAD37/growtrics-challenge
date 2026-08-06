"""Both backends still fit the ports they claim.

`isinstance` against a `Protocol` checks method names and stops there, so an adapter whose `list`
had grown a keyword argument would satisfy it and fail at the first call. `assert_conforms`
compares parameter names and kinds as well, which is what makes this cheap to run and worth
running: the ports are structural, so nothing else in the build would notice the drift.

Parameterised over the backends like every other file here, so the doubles and the SQL adapters
are held to the same signatures rather than to each other's.
"""

from support.backends import StorageBackend
from support.rows import assert_conforms

from app.access.ports import PrincipalRepository
from app.custody.ports import ArtifactWriter
from app.orchestration.ports import (
    Clock,
    JobRepository,
    RequestStore,
    UnitOfWork,
    WorkQueue,
)


def test_the_backend_conforms_to_every_port_it_stands_for(backend: StorageBackend) -> None:
    assert_conforms(backend.clock, Clock)
    assert_conforms(backend.principals, PrincipalRepository)
    assert_conforms(backend.requests, RequestStore)
    assert_conforms(backend.jobs, JobRepository)
    assert_conforms(backend.unit_of_work, UnitOfWork)
    assert_conforms(backend.queue, WorkQueue)
    assert_conforms(backend.artifacts, ArtifactWriter)
