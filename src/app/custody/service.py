"""Custody from the outside: the write path a run takes, and the read path a client takes.

Two seams, one class, because they are two halves of one responsibility: custody is the sole
writer of `artifacts` and of the object store (D066), and therefore the only thing entitled to
say what a stored artifact is. `orchestration.ports.ArtifactWriter` and
`orchestration.ports.ArtifactReader` declare both shapes and `Custody` satisfies them
structurally, so neither package imports the other.

The write path is `harvest`, and its order is the trust boundary:

1. Resolve the contract from the profile. One object, and the same one the worker was handed
   (D077), so the acceptance test is the statement of work read backwards.
2. `Harvester` parses the worker's document through `generation.acl`, refuses every path that is
   not a plain name under the harvest root, and only then pulls the bytes -- capped, one file at
   a time, hashed by us.
3. `ResultValidator` runs the part's named check chain over those bytes and returns a verdict.
   @audit most of that chain is a stub in this build; see `custody/verifier.py`.
4. `require_complete` asks the second half of the contract question: is every required part
   present *and* clean. A primary that arrived and failed is as undeliverable as one that never
   arrived.
5. `ArtifactPublisher` writes bytes then rows, in that order, quarantined parts included. A
   failing candidate is stored with its verdict and no public route, because the evidence is what
   makes a failure diagnosable later.

The read path is `list` and `open_content`, and its rule is that no refusal is invented here:
the listing's two predicates live in the index (D073, A6), and `content_stream` is what refuses a
quarantined or operator-audience row.
"""

from typing import Final

from app.custody.harvester import Harvester
from app.custody.ports import ArtifactRepository, ArtifactStore, ReadsTheClock, ResultValidator
from app.custody.store import ArtifactPublisher, content_stream
from app.custody.verifier import require_complete
from app.domain.access import AccessScope
from app.domain.artifact import GenerationOutcome, HarvestOutcome
from app.domain.contracts import OutputContract, contract_for
from app.domain.enums import ArtifactRole, ProfileId
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import ArtifactId, ChatContextId, JobId, PrincipalId
from app.domain.records import ArtifactRecord, ContentStream, Cursor, Page

__all__ = ["Custody"]


class Custody:
    """`ArtifactWriter` on the way in, `ArtifactReader` on the way out."""

    def __init__(
        self,
        *,
        harvester: Harvester,
        validator: ResultValidator,
        store: ArtifactStore,
        artifacts: ArtifactRepository,
        clock: ReadsTheClock,
    ) -> None:
        self._harvester: Final[Harvester] = harvester
        self._validator: Final[ResultValidator] = validator
        self._store: Final[ArtifactStore] = store
        self._artifacts: Final[ArtifactRepository] = artifacts
        self._clock: Final[ReadsTheClock] = clock
        self._publisher: Final[ArtifactPublisher] = ArtifactPublisher(store, artifacts)

    # ----------------------------------------------------------------- the write path

    async def harvest(
        self,
        *,
        job_id: JobId,
        principal_id: PrincipalId,
        chat_context_id: ChatContextId | None,
        profile: ProfileId,
        max_duration_s: int,
        outcome: GenerationOutcome,
    ) -> HarvestOutcome:
        """Fetch the claimed bytes, verify them, store them, insert the rows."""
        contract = contract_for(profile)
        harvested = await self._harvester.harvest(
            outcome.document, contract, session_id=outcome.session_id
        )
        verified = tuple(
            self._validator.verify(item, contract, requested_max_duration_s=max_duration_s)
            for item in harvested
        )
        require_complete(verified, contract)

        records = await self._publisher.publish_all(
            verified,
            job_id=job_id,
            principal_id=principal_id,
            chat_context_id=chat_context_id,
            now=self._clock.now(),
        )
        return HarvestOutcome(primary=_primary_of(records), artifacts=records)

    async def publish(self, primary: ArtifactRecord) -> ArtifactRecord:
        """Confirm the primary is a learner's to fetch, and hand the row back.

        `artifact_record_for` stamps `published_at` at insert, because what it encodes -- clean,
        and meant for a learner -- is already decided by the time a row exists, and a row written
        unpublished is a row a crash between the two writes leaves invisible forever. So this
        method asserts rather than writes, and the assertion is worth its line: a primary that is
        not publishable stops the run here instead of becoming a `SUCCEEDED` job pointing at bytes
        no client may fetch.

        @TODO this becomes a real write when `GET /v1/jobs/{id}/deliverable` lands and the sidecar
        roles are published as a set (`orchestration/steps/publish.py`). That needs one `UPDATE`
        statement in `app/storage/sql/repositories.py` and a line in the inventory in
        `tests/unit/storage/sql_discipline_test.py`.
        """
        if primary.published_at is None:
            raise DomainError(
                ErrorCode.ARTIFACT_NOT_READY,
                {"reason": "not_publishable", "verdict": primary.scan_verdict.value},
            )
        return primary

    # ----------------------------------------------------------------- the read path

    async def list(
        self,
        scope: AccessScope,
        *,
        job_id: JobId | None,
        cursor: Cursor | None,
        limit: int,
    ) -> Page[ArtifactRecord]:
        """`LEARNER` audience and `CLEAN` verdict only; the index carries both predicates.

        @audit no ownership check. Any caller lists any artifact (scope override item 1).
        """
        return await self._artifacts.list(scope, job_id=job_id, cursor=cursor, limit=limit)

    async def open_content(
        self, scope: AccessScope, artifact_id: ArtifactId
    ) -> ContentStream | None:
        """Open the bytes for streaming. `None` when there is no such row at all.

        `None` and a refusal are different answers and both reach the client as `404`: no row
        means nothing was ever made, and `content_stream` raising means the row exists and is not
        a learner's to read. Keeping them apart here is what lets the second one become a `403`
        the day there is an operator surface to serve it to.

        A row whose bytes are gone is `ARTIFACT_NOT_READY`, raised by the store on the first
        chunk rather than here: asking S3 whether an object exists is a round trip this endpoint
        would pay on every download to catch a case that should not happen.

        @audit no ownership check. Any caller streams any artifact.
        """
        record = await self._artifacts.get(scope, artifact_id)
        if record is None:
            return None
        return content_stream(record, self._store.open(record.storage_uri), _serving_contract())


def _primary_of(records: tuple[ArtifactRecord, ...]) -> ArtifactRecord:
    """The row `jobs.artifact_id` will name.

    `require_complete` has already established that a clean primary is in the set, so reaching
    the raise means the contract and this function disagree about what "required" meant, which is
    a bug on our side rather than a worker's doing.
    """
    for record in records:
        if record.role is ArtifactRole.PRIMARY:
            return record
    raise DomainError(ErrorCode.GENERATION_FAILED, {"reason": "no_primary_after_verification"})


def _serving_contract() -> OutputContract:
    """The serving policy a download is answered under.

    @TODO `artifacts` carries no profile column, so this resolves the one enabled contract
    (`domain/contracts.py`, D081) rather than the one the job asked for. With a second profile
    enabled this has to read the job's, which means either a column on `artifacts` or a join --
    a decision, and one the frozen schema (D071) has to be amended for.
    """
    return contract_for(ProfileId.VIDEO_SHORT_V1)
