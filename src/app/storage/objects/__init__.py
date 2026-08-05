"""Long-term artifact store: MinIO in compose, S3 later, filesystem in tests (D052).

`ports.py` holds the seam and the key rules every backend shares. `filesystem.py` is the backend
that works today; `s3.py` is D096's backend and is a declared stub, so the composition root has
exactly one real choice and no way to pick a plausible-looking nothing.
"""

from app.storage.objects.filesystem import FILESYSTEM_SCHEME, FilesystemObjectStore
from app.storage.objects.ports import (
    OBJECT_CHUNK_BYTES,
    ObjectKey,
    ObjectStat,
    ObjectStore,
    StorageUri,
    UnknownStorageUriError,
    UnsafeObjectKeyError,
    build_storage_uri,
    ensure_safe_key,
    missing_object,
    parse_storage_uri,
)
from app.storage.objects.s3 import S3_SCHEME, S3ObjectStore

__all__ = [
    "FILESYSTEM_SCHEME",
    "OBJECT_CHUNK_BYTES",
    "S3_SCHEME",
    "FilesystemObjectStore",
    "ObjectKey",
    "ObjectStat",
    "ObjectStore",
    "S3ObjectStore",
    "StorageUri",
    "UnknownStorageUriError",
    "UnsafeObjectKeyError",
    "build_storage_uri",
    "ensure_safe_key",
    "missing_object",
    "parse_storage_uri",
]
