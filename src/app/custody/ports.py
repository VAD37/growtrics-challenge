"""Custody's ports. Four protocols, no adapters.

Custody is the only module that writes bytes and the only module that writes `artifacts`
(D066). It reads jobs by id for scoping and never writes job status: it returns a verified
artifact and orchestration decides what that means, which is what keeps invariant 1 of
`VideoJob` checkable in one place.

Everything below is `typing.Protocol`, so an adapter satisfies one by having the methods. That
matters twice over here: `CandidateSource` is satisfied by `generation.GenerationBackend`
without custody depending on that class, and `ArtifactStore` and `ArtifactWriter` are satisfied
by the SQL and object-store adapters without this package importing `app.storage`.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.access import AccessScope
from app.domain.artifact import HarvestedFile, VerifiedArtifact
from app.domain.contracts import OutputContract
from app.domain.ids import ArtifactId, JobId
from app.domain.records import ArtifactRecord, Cursor, Page


@runtime_checkable
class CandidateSource(Protocol):
    """Where harvested bytes come from. Pull, never push (`plan/06-trust-boundary.md`).

    Structurally identical to `GenerationBackend.fetch`. The worker never sends us anything and
    never writes to our storage; we ask for exactly the paths its manifest declared, one at a
    time, with a cap.

    An implementation over a real filesystem must open without following symlinks. The ACL can
    only judge the shape of a path string, so refusing a link is this port's obligation.
    """

    async def fetch(self, session_id: str, rel_path: str, *, max_bytes: int) -> bytes: ...


@runtime_checkable
class ResultValidator(Protocol):
    """The versioned acceptance test (D064).

    `version` is recorded on every artifact, so upgrading the chain becomes a swap comparable
    against the old verdicts rather than an edit spread through custody.
    """

    version: str

    def verify(
        self,
        harvested: HarvestedFile,
        contract: OutputContract,
        *,
        requested_max_duration_s: int,
    ) -> VerifiedArtifact: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """The object store. Bytes live here; the database holds the row that points at them.

    `put` returns the `storage_uri` that goes on the row. Content addressing means the backend
    can change without touching a row: the key is derived from the hash, not from a sequence.
    """

    async def put(self, key: str, data: bytes, *, media_type: str) -> str: ...

    def open(self, storage_uri: str) -> AsyncIterator[bytes]: ...


@runtime_checkable
class ArtifactWriter(Protocol):
    """Inserts the `artifacts` row. The one table custody owns.

    Split from `ArtifactStore` because the two failure modes are different: a byte write that
    half succeeded leaves an orphan object, and a row insert that fails leaves bytes nobody can
    reach. Custody writes bytes first and the row second, so the reachable state is always "row
    implies bytes".
    """

    async def insert(self, record: ArtifactRecord) -> ArtifactRecord: ...


@runtime_checkable
class ArtifactRepository(ArtifactWriter, Protocol):
    """`ArtifactWriter` plus the two reads the serving path needs.

    One port rather than two, because `artifacts` has one owner and both storage backends already
    implement all three methods. `ArtifactWriter` stays as the narrower name for the write half,
    which is what `custody.store.ArtifactPublisher` takes: a publisher that could list rows is a
    publisher somebody eventually reads with.

    Every read takes an `AccessScope` first (D067). @audit none of them consults it in this build
    (scope override item 1); see `app/storage/sql/repositories.py`.
    """

    async def get(self, scope: AccessScope, artifact_id: ArtifactId) -> ArtifactRecord | None:
        """The row behind a content request, verdict and audience included.

        Unfiltered on purpose: `custody.store.content_stream` is what refuses a quarantined or
        operator-audience row, and it needs to see one to refuse it.
        """
        ...

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        """`LEARNER` and `CLEAN` only, newest first, optionally one job's output (D073, A6)."""
        ...


@runtime_checkable
class ReadsTheClock(Protocol):
    """`orchestration.ports.Clock`, restated.

    Restated rather than imported because the dependency between these two packages points one
    way: orchestration reaches custody, never the reverse. Structural typing means the composition
    root hands both the same object, so a frozen clock in a test freezes `artifacts.created_at`
    and `jobs.updated_at` together.
    """

    def now(self) -> datetime: ...
