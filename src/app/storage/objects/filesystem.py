"""A working object store on a local directory. The one that actually runs today.

`s3.py` is a declared stub, so this is the store the demo uses: a directory per bucket under a
root, one file per object, keys taken verbatim from `custody.store.object_key`. It is a real
adapter rather than a test double -- it creates its own bucket, writes atomically, streams reads
in chunks off the event loop, and refuses a key that would leave the bucket.

Two properties worth stating outright.

**It creates the bucket.** Nothing in `docker-compose.yml` creates one, so a store that assumed
the bucket existed would fail on the first artifact of a clean checkout. `put` makes the
directory on the way past, which is idempotent and needs no start-up ordering.

**A write is atomic or absent.** Bytes go to a uniquely named temporary file and are renamed
into place, so a crash mid-write leaves a `.part` file rather than a truncated video that hashes
to nothing anybody expects. Content-addressed keys mean two writers of one object write
identical bytes, and the rename is the only ordering they need to agree on.

Every blocking call goes through `asyncio.to_thread`. This runs inside the API process, and a
64 MiB read on the event loop would stall every other request for the length of the download.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final
from uuid import uuid4

from app.config import settings
from app.storage.objects.ports import (
    OBJECT_CHUNK_BYTES,
    ObjectStat,
    StorageUri,
    UnsafeObjectKeyError,
    build_storage_uri,
    ensure_safe_key,
    missing_object,
    parse_storage_uri,
)

__all__ = ["FILESYSTEM_SCHEME", "FilesystemObjectStore"]

FILESYSTEM_SCHEME: Final[str] = "file"
"""The scheme on every uri this backend writes, and the only one it will read."""


def _write_atomically(path: Path, data: bytes) -> None:
    """Write `data` to `path` through a temporary neighbour, then rename.

    Runs in a worker thread. The temporary name carries random bytes so two writers of the same
    content-addressed object cannot truncate each other's half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.part"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class FilesystemObjectStore:
    """`ObjectStore` over a directory. Satisfies `custody.ports.ArtifactStore` as well."""

    def __init__(
        self,
        root: Path,
        *,
        bucket: str = settings.object_store_bucket,
        chunk_bytes: int = OBJECT_CHUNK_BYTES,
    ) -> None:
        self._root: Path = Path(root)
        self._bucket: str = bucket
        self._bucket_root: Path = self._root / bucket
        self._chunk_bytes: int = chunk_bytes

    @property
    def bucket_root(self) -> Path:
        """Where objects land. Exposed so a test can look at the disk rather than at this class."""
        return self._bucket_root

    async def ensure_bucket(self) -> None:
        """Create the bucket directory if it is absent (D096).

        Called by `put` on every write, and callable by a composition root that wants the
        failure at start-up instead of on the first artifact.
        """
        await asyncio.to_thread(self._bucket_root.mkdir, parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        """Store the bytes and return the uri the `artifacts` row will carry.

        `media_type` is unused here: a directory has nowhere to record one, and `artifacts.mime`
        is what the response's `Content-Type` comes from anyway.
        """
        del media_type
        path = self._path_for(ensure_safe_key(key))
        await self.ensure_bucket()
        await asyncio.to_thread(_write_atomically, path, data)
        return build_storage_uri(scheme=FILESYSTEM_SCHEME, bucket=self._bucket, key=key)

    def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        """Open for streaming, refusing a missing object here rather than mid-response."""
        path = self._path_for(self._key_of(storage_uri))
        if not path.is_file():
            raise missing_object(storage_uri)
        return self._stream(path)

    async def stat(self, storage_uri: str) -> ObjectStat | None:
        path = self._path_for(self._key_of(storage_uri))
        try:
            info = await asyncio.to_thread(path.stat)
        except OSError:
            return None
        return ObjectStat(storage_uri=storage_uri, size_bytes=info.st_size)

    async def _stream(self, path: Path) -> AsyncIterator[bytes]:
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, self._chunk_bytes)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    def _key_of(self, storage_uri: StorageUri) -> str:
        return parse_storage_uri(storage_uri, scheme=FILESYSTEM_SCHEME, bucket=self._bucket)

    def _path_for(self, key: str) -> Path:
        """The file a key names, or a refusal.

        The string check has already run; this is the second half of it. `resolve` follows every
        symlink on the way, so a link planted inside the bucket that points at `/etc` is caught
        here even though its key was a perfectly ordinary relative path.
        """
        candidate = (self._bucket_root / key).resolve()
        if not candidate.is_relative_to(self._bucket_root.resolve()):
            raise UnsafeObjectKeyError("object key resolves outside its bucket")
        return candidate
