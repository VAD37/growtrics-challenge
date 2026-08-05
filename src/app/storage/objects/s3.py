"""The S3 backend for MinIO (D096). DEFERRED: signatures only, no working method.

D096 picks `boto3` over the S3 API so that MinIO in compose and S3 later are one client. That
decision stands and this file is where it lands. It is not built.

**The consequence, stated rather than hidden: the `storage` service in `docker-compose.yml` is
unused.** Until these four bodies exist, the composition root has to hand custody a
`FilesystemObjectStore` pointed at a volume, and every artifact of a demo run lives on the API
container's disk rather than in the object store beside it. Nothing silently succeeds -- every
method below raises -- so a wiring that reaches for this adapter fails at the first artifact
instead of writing bytes nowhere.

`boto3` is already a dependency (`pyproject.toml`), so picking this up adds nothing to install.
Each body is small and the shapes are decided:

- `ensure_bucket`: `head_bucket`, and on a `404`/`NoSuchBucket` a `create_bucket`. Nothing else
  in compose creates it, which is the same obligation `FilesystemObjectStore` already meets.
- `put`: `put_object(Bucket=..., Key=..., Body=data, ContentType=media_type)`, returning
  `build_storage_uri(scheme=S3_SCHEME, bucket=..., key=...)`.
- `open`: `get_object`, then read the `StreamingBody` in `OBJECT_CHUNK_BYTES` chunks. Every
  call is blocking and belongs in `asyncio.to_thread`, exactly as the filesystem backend does
  it, because this runs inside the API process.
- `stat`: `head_object`, `None` on a `404`.

The client is a constructor argument rather than a module global so that the endpoint,
credentials and bucket resolve once at composition time and a test can pass a double.
"""

from collections.abc import AsyncIterator
from typing import Final

from app.config import settings
from app.storage.objects.ports import ObjectStat

__all__ = ["S3_SCHEME", "S3ObjectStore"]

S3_SCHEME: Final[str] = "s3"
"""The scheme this backend would write on every uri. Kept here so rows agree before it exists."""

_DEFERRED: Final[str] = "the S3 object store is deferred; see app/storage/objects/s3.py"


class S3ObjectStore:
    """`ObjectStore` over MinIO or S3. @TODO DEFERRED, see the module docstring.

    The constructor is real: it resolves configuration and holds it, so wiring this adapter is
    a composition change rather than a rewrite once the bodies land.
    """

    def __init__(
        self,
        *,
        endpoint: str = settings.object_store_endpoint,
        access_key: str = settings.object_store_access_key,
        secret_key: str = settings.object_store_secret_key,
        bucket: str = settings.object_store_bucket,
    ) -> None:
        self._endpoint: str = endpoint
        self._access_key: str = access_key
        # @audit credentials live on this instance for the life of the process. The demo's
        # defaults are `minioadmin`/`minioadmin` (`app/config.py`), which is a laptop credential
        # and nothing more; a deployment supplies its own and this class never logs either.
        self._secret_key: str = secret_key
        self._bucket: str = bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        """@TODO DEFERRED. Create `settings.object_store_bucket` when it is absent.

        Nothing else in compose creates the bucket, so a backend that skipped this would fail
        on the first artifact of a clean checkout.
        """
        raise NotImplementedError(_DEFERRED)

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        """@TODO DEFERRED. Store the bytes and return the uri for the `artifacts` row.

        Raises rather than returning a plausible uri: a row pointing at bytes nobody wrote is a
        link a client follows to nothing, and no later sweep can repair it.
        """
        raise NotImplementedError(_DEFERRED)

    def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        """@TODO DEFERRED. Stream the object in `OBJECT_CHUNK_BYTES` chunks."""
        raise NotImplementedError(_DEFERRED)

    async def stat(self, storage_uri: str) -> ObjectStat | None:
        """@TODO DEFERRED. Size without reading, `None` when the object is absent."""
        raise NotImplementedError(_DEFERRED)
