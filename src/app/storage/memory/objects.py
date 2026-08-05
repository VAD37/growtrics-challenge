"""The object store in a dictionary, for tests that still want the real contract.

Bytes are held in this object and not in `MemoryDatabase`, deliberately. D052 says the database
holds metadata and a pointer and the store holds files; a double that kept both in one place
would make the two stores look like one, and the first thing to break on a real deployment is
whatever the doubles let you forget.

It runs the same key rules as `FilesystemObjectStore`. A key that the filesystem backend would
refuse is refused here too, so a test passing against this store is not passing because the
store was permissive.
"""

from collections.abc import AsyncIterator
from typing import Final

from app.storage.objects.ports import (
    OBJECT_CHUNK_BYTES,
    ObjectStat,
    StorageUri,
    build_storage_uri,
    ensure_safe_key,
    missing_object,
    parse_storage_uri,
)

__all__ = ["MEMORY_SCHEME", "MemoryObjectStore"]

MEMORY_SCHEME: Final[str] = "memory"
"""Its own scheme, so a `storage_uri` written by a test is never mistaken for a real object."""


class MemoryObjectStore:
    """`ObjectStore` over a dict. Satisfies `custody.ports.ArtifactStore` as well."""

    def __init__(
        self,
        *,
        bucket: str = "artifacts",
        chunk_bytes: int = OBJECT_CHUNK_BYTES,
    ) -> None:
        self._bucket: str = bucket
        self._chunk_bytes: int = chunk_bytes
        self._objects: dict[StorageUri, bytes] = {}
        self.media_types: dict[StorageUri, str] = {}
        """What each `put` claimed. Kept so a test can assert custody passed the row's mime."""

    def __len__(self) -> int:
        return len(self._objects)

    def bytes_at(self, storage_uri: StorageUri) -> bytes | None:
        """The stored object, whole. For assertions; the port streams."""
        return self._objects.get(storage_uri)

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        storage_uri = build_storage_uri(
            scheme=MEMORY_SCHEME, bucket=self._bucket, key=ensure_safe_key(key)
        )
        self._objects[storage_uri] = data
        self.media_types[storage_uri] = media_type
        return storage_uri

    def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        """Stream the object, refusing a missing one at call time as the real backends do."""
        data = self._objects.get(self._known(storage_uri))
        if data is None:
            raise missing_object(storage_uri)
        return self._stream(data)

    async def stat(self, storage_uri: str) -> ObjectStat | None:
        data = self._objects.get(self._known(storage_uri))
        if data is None:
            return None
        return ObjectStat(storage_uri=storage_uri, size_bytes=len(data))

    async def _stream(self, data: bytes) -> AsyncIterator[bytes]:
        for start in range(0, len(data), self._chunk_bytes):
            yield data[start : start + self._chunk_bytes]

    def _known(self, storage_uri: StorageUri) -> StorageUri:
        """Refuse a uri from another scheme or bucket before looking it up."""
        parse_storage_uri(storage_uri, scheme=MEMORY_SCHEME, bucket=self._bucket)
        return storage_uri
