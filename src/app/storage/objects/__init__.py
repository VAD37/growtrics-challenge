"""Long-term artifact store: MinIO in compose, S3 later, filesystem in tests (D052).

`ports.py` holds the seam and the key rules every backend shares. Both backends work:
`filesystem.py` writes to a directory, `s3.py` is D096's MinIO client. Only the second can hand
out a presigned url, which is why that capability is `PresigningObjectStore` and not a method on
`ObjectStore` -- a caller asks whether the store it was given has it.
"""

from app.storage.objects.filesystem import FILESYSTEM_SCHEME, FilesystemObjectStore
from app.storage.objects.ports import (
    OBJECT_CHUNK_BYTES,
    ObjectKey,
    ObjectStat,
    ObjectStore,
    PresigningObjectStore,
    StorageUri,
    UnknownStorageUriError,
    UnsafeObjectKeyError,
    build_storage_uri,
    ensure_safe_key,
    missing_object,
    parse_storage_uri,
)
from app.storage.objects.s3 import S3_REGION, S3_SCHEME, S3Client, S3ClientFactory, S3ObjectStore

__all__ = [
    "FILESYSTEM_SCHEME",
    "OBJECT_CHUNK_BYTES",
    "S3_REGION",
    "S3_SCHEME",
    "FilesystemObjectStore",
    "ObjectKey",
    "ObjectStat",
    "ObjectStore",
    "PresigningObjectStore",
    "S3Client",
    "S3ClientFactory",
    "S3ObjectStore",
    "StorageUri",
    "UnknownStorageUriError",
    "UnsafeObjectKeyError",
    "build_storage_uri",
    "ensure_safe_key",
    "missing_object",
    "parse_storage_uri",
]
