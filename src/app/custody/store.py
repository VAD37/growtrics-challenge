"""Where verified bytes go, and the row that points at them.

Custody is the only module that opens the object store and the only writer of `artifacts`
(D066). It does not write job status: it returns rows and orchestration decides what they mean.

Bytes are not in the database. `storage_uri` points at the object store and `content_hash` is
the addressing scheme, so the storage backend can change without touching a row.

The adapters live in `app/storage` and are somebody else's file. This module holds the port's
consumers: the key derivation, the record mapping, and the two-step publish.
"""

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from app.custody.ports import ArtifactStore, ArtifactWriter
from app.domain.artifact import VerifiedArtifact
from app.domain.contracts import OutputContract
from app.domain.enums import Audience, ScanVerdict
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import ArtifactId, ChatContextId, JobId, PrincipalId, derive_artifact_id
from app.domain.records import ArtifactRecord, ContentStream

PUBLIC_PREFIX: Final[str] = "artifacts"
QUARANTINE_PREFIX: Final[str] = "quarantine"
"""A failing candidate is stored and has no public route. Operators can inspect it.

Separate prefixes rather than a flag on the key, so a misconfigured bucket policy or a
mistakenly public listing cannot expose quarantined bytes alongside clean ones.
"""

_DEFAULT_SUFFIX: Final[str] = ".bin"

SUFFIXES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "video/mp4": ".mp4",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "text/plain": ".txt",
        "text/vtt": ".vtt",
        "application/x-ndjson": ".jsonl",
    }
)
"""Cosmetic only.

@audit nothing trusts a suffix. The media type on the row comes from the contract's part spec,
the response's `Content-Type` comes from the row, and `X-Content-Type-Options: nosniff` is in
the serving policy. The extension exists so an object browser is readable by a human.
"""


def object_key(
    job_id: JobId,
    content_hash: str,
    media_type: str,
    verdict: ScanVerdict,
) -> str:
    """Content-addressed, under the job, under a prefix that encodes the verdict."""
    prefix = PUBLIC_PREFIX if verdict is ScanVerdict.CLEAN else QUARANTINE_PREFIX
    digest = content_hash.removeprefix("sha256:")
    suffix = SUFFIXES.get(media_type, _DEFAULT_SUFFIX)
    return f"{prefix}/{job_id}/{digest}{suffix}"


def artifact_record_for(
    verified: VerifiedArtifact,
    *,
    job_id: JobId,
    principal_id: PrincipalId,
    chat_context_id: ChatContextId | None,
    storage_uri: str,
    now: datetime,
) -> ArtifactRecord:
    """Map a verdict onto the `artifacts` row.

    `principal_id` (A6, D088) and `chat_context_id` (D073) are copied at insert rather than
    joined at read, which is what makes listing a learner's artifacts one index scan whether or
    not a chat context exists. Copying correctly is this function's whole responsibility, and it
    is the reason exactly one module inserts artifacts.

    `published_at` is set only for a clean, learner-facing file. It is the field that says "a
    client may see this", separate from `scan_verdict`, which says "this is not dangerous", and
    from `audience`, which says "this is theirs".
    """
    descriptor = verified.harvested.descriptor
    publishable = verified.verdict is ScanVerdict.CLEAN and descriptor.audience is Audience.LEARNER
    return ArtifactRecord(
        artifact_id=derive_artifact_id(job_id, verified.harvested.content_hash),
        job_id=job_id,
        principal_id=principal_id,
        chat_context_id=chat_context_id,
        role=descriptor.role,
        audience=descriptor.audience,
        mime=descriptor.media_type,
        rel_path=descriptor.rel_path,
        size_bytes=verified.harvested.size_bytes,
        content_hash=verified.harvested.content_hash,
        storage_uri=storage_uri,
        probe=verified.probe,
        scan_verdict=verified.verdict,
        validator_version=verified.validator_version,
        published_at=now if publishable else None,
        created_at=now,
    )


class ArtifactPublisher:
    """Writes bytes, then the row. In that order, always.

    A byte write that half succeeded leaves an object nobody references, which a sweep can
    collect. A row written before its bytes is a link a client can follow to a 404, which
    nothing can repair after the fact.

    @TODO the two writes are not one transaction and cannot be: the object store is not the
    database (D048). The orphan direction is the recoverable one, and the sweep that collects
    orphans is unbuilt (`docs/plan/10-scope-matrix.md`, artifact lifecycle).
    """

    def __init__(self, store: ArtifactStore, writer: ArtifactWriter) -> None:
        self._store: ArtifactStore = store
        self._writer: ArtifactWriter = writer

    async def publish(
        self,
        verified: VerifiedArtifact,
        *,
        job_id: JobId,
        principal_id: PrincipalId,
        chat_context_id: ChatContextId | None,
        now: datetime,
    ) -> ArtifactRecord:
        harvested = verified.harvested
        key = object_key(
            job_id,
            harvested.content_hash,
            harvested.descriptor.media_type,
            verified.verdict,
        )
        storage_uri = await self._store.put(
            key,
            harvested.data,
            media_type=harvested.descriptor.media_type,
        )
        record = artifact_record_for(
            verified,
            job_id=job_id,
            principal_id=principal_id,
            chat_context_id=chat_context_id,
            storage_uri=storage_uri,
            now=now,
        )
        return await self._writer.insert(record)

    async def publish_all(
        self,
        verified: tuple[VerifiedArtifact, ...],
        *,
        job_id: JobId,
        principal_id: PrincipalId,
        chat_context_id: ChatContextId | None,
        now: datetime,
    ) -> tuple[ArtifactRecord, ...]:
        """Publish a whole deliverable, quarantined parts included.

        A failing candidate is stored with its verdict and no public route, because the evidence
        is what makes a failure diagnosable later.
        """
        records: list[ArtifactRecord] = []
        for item in verified:
            records.append(
                await self.publish(
                    item,
                    job_id=job_id,
                    principal_id=principal_id,
                    chat_context_id=chat_context_id,
                    now=now,
                )
            )
        return tuple(records)


def content_stream(
    record: ArtifactRecord,
    chunks: AsyncIterator[bytes],
    contract: OutputContract,
) -> ContentStream:
    """Wrap an open artifact for the response, with the contract's serving policy applied.

    Two refusals, both `ARTIFACT_NOT_FOUND` rather than a 403: an operator-audience row and a
    quarantined row are not things a client may learn the existence of. This is the second
    place that rule is enforced -- the listing index already excludes both -- because the read
    path must not depend on a query staying correct.
    """
    if record.audience is not Audience.LEARNER or record.scan_verdict is not ScanVerdict.CLEAN:
        raise DomainError(ErrorCode.ARTIFACT_NOT_FOUND)
    return ContentStream(
        media_type=record.mime,
        size_bytes=record.size_bytes,
        content_hash=record.content_hash,
        filename=_filename_for(record.artifact_id, record.mime),
        disposition=contract.serving.disposition,
        chunks=chunks,
    )


def _filename_for(artifact_id: ArtifactId, media_type: str) -> str:
    """A download name that is ours, not the worker's.

    The worker's `rel_path` never becomes a filename. It is attacker-influenced text that would
    end up in a `Content-Disposition` header, which is a header-injection and a path-confusion
    problem at once.
    """
    return f"{artifact_id}{SUFFIXES.get(media_type, _DEFAULT_SUFFIX)}"
