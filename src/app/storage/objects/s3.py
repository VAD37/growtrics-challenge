"""The S3 backend for MinIO now and S3 later (D096). Working, not a stub.

D096 picks `boto3` over the S3 API so that MinIO in compose and S3 in a deployment are one
client and one code path. This is that client. With it the `storage` service in
`docker-compose.yml` stops being decoration: artifact bytes land in the object store instead of
on the API container's disk, and an 80 MiB download can leave our process entirely.

Four properties are worth stating outright, because three of them are the bugs this file exists
to not have.

**It creates the bucket.** Nothing in `docker-compose.yml` does, and no fifth service exists to
do it, so a backend that assumed the bucket existed would fail on the first artifact of a clean
checkout. `ensure_bucket` runs `head_bucket` and creates on absence; `put` calls it, and it
remembers, so the check is one round trip per process rather than one per write.

**Nothing blocking runs on the event loop.** `boto3` is synchronous and this adapter runs inside
the API process, where one blocking S3 call stalls every other request for the length of the
transfer. Every `boto3` call therefore sits in a `_blocking` method, and every `_blocking` method
has exactly one caller: `asyncio.to_thread`. Building a client counts -- `boto3.client` loads
service model JSON off disk -- so `_client_for` is lazy and is itself only ever reached from a
worker thread. `tests/unit/storage/objects_s3_test.py` asserts that rather than trusting it.

**Reads stream.** `get_object` hands back a body that is read in `OBJECT_CHUNK_BYTES` pieces, so
a 64 MiB video never sits whole in this process. `put` still takes whole bytes, because
`custody.ports.ArtifactStore` declares it that way and custody has already hashed and verified
the complete object before it stores anything.

**There are two clients, and which one signs matters.** See `presigned_url`.

Key rules are not reimplemented here. `ports.ensure_safe_key` and the storage-uri helpers are the
one policy every backend shares, and a uri naming somebody else's bucket is refused by
`parse_storage_uri` in a synchronous frame, before any thread or any network call.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Final, Protocol, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings
from app.storage.objects.ports import (
    OBJECT_CHUNK_BYTES,
    ObjectStat,
    StorageUri,
    build_storage_uri,
    ensure_safe_key,
    missing_object,
    parse_storage_uri,
)

__all__ = ["S3_REGION", "S3_SCHEME", "ByteStream", "S3Client", "S3ClientFactory", "S3ObjectStore"]

S3_SCHEME: Final[str] = "s3"
"""The scheme this backend writes on every uri, and the only one it will read."""

S3_REGION: Final[str] = "us-east-1"
"""SigV4 signs over a region, so one has to be named even when the store ignores it.

MinIO accepts any region. This is not a setting because changing it would invalidate every url
already signed and buy nothing: a real S3 deployment sets `AWS_DEFAULT_REGION` on the client it
composes instead.
"""

_ABSENT_CODES: Final[frozenset[str]] = frozenset({"404", "NotFound", "NoSuchKey", "NoSuchBucket"})
"""What "there is no such thing" is spelled, across the two stores and the two verbs.

`get_object` on a real S3 says `NoSuchKey`; `head_object` has no response body to put a code in
and says a bare `404`; MinIO says `NoSuchBucket` where S3 says `NotFound`. Guessing one of them
is how a missing object turns into a 500.
"""

_BUCKET_EXISTS_CODES: Final[frozenset[str]] = frozenset(
    {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}
)
"""Two processes starting at once both create the bucket. The loser has still won."""


class ByteStream(Protocol):
    """The `Body` of a `get_object` response: read it in pieces, then close it.

    Named as a protocol rather than typed as `botocore.response.StreamingBody` so a test can
    hand this adapter a double without importing botocore's internals.
    """

    def read(self, amt: int) -> bytes: ...

    def close(self) -> None: ...


class S3Client(Protocol):
    """The five calls this adapter makes, and nothing else.

    `boto3` clients are generated at runtime and have no static type, so this is what stands in
    for one. It doubles as the honest list of what a credential handed to this adapter is used
    for: read one object, write one object, ask about one object or bucket, make one bucket.
    """

    def head_bucket(self, *, Bucket: str) -> Mapping[str, object]: ...

    def create_bucket(self, *, Bucket: str) -> Mapping[str, object]: ...

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> Mapping[str, object]: ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...

    def generate_presigned_url(
        self, *, ClientMethod: str, Params: Mapping[str, str], ExpiresIn: int
    ) -> str: ...


type S3ClientFactory = Callable[[str], S3Client]
"""Endpoint in, client out. Credentials are closed over, so a caller cannot mix two stores."""


def _absent(error: ClientError) -> bool:
    """Whether a botocore failure means "no such thing" rather than "the store is unhappy".

    The status code is checked as well as the string, so a fifth spelling of absence from some
    other S3-compatible store still reads as absence instead of surfacing as a 500.
    """
    body = cast(Mapping[str, object], error.response.get("Error", {}))
    if str(body.get("Code", "")) in _ABSENT_CODES:
        return True
    metadata = cast(Mapping[str, object], error.response.get("ResponseMetadata", {}))
    return metadata.get("HTTPStatusCode") == 404


def _error_code(error: ClientError) -> str:
    body = cast(Mapping[str, object], error.response.get("Error", {}))
    return str(body.get("Code", ""))


def _client_factory(*, access_key: str, secret_key: str, region: str) -> S3ClientFactory:
    """A factory that builds a real `boto3` client for whichever endpoint it is handed.

    Path addressing, not virtual-host: MinIO does not serve `bucket.host` by default, and a
    presigned url whose bucket became a subdomain of `localhost` resolves nowhere at all.

    SigV4 explicitly, because it is what MinIO verifies and what makes a presigned url carry its
    own expiry rather than an unbounded signature.
    """

    def build(endpoint: str) -> S3Client:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cast(S3Client, client)

    return build


class S3ObjectStore:
    """`ObjectStore` and `PresigningObjectStore` over MinIO or S3.

    Satisfies `custody.ports.ArtifactStore` as well: same `put`, same `open`.
    """

    def __init__(
        self,
        *,
        endpoint: str = settings.object_store_endpoint,
        public_endpoint: str = settings.object_store_public_endpoint,
        access_key: str = settings.object_store_access_key,
        secret_key: str = settings.object_store_secret_key,
        bucket: str = settings.object_store_bucket,
        presign_ttl_seconds: int = settings.object_store_presign_ttl_seconds,
        chunk_bytes: int = OBJECT_CHUNK_BYTES,
        region: str = S3_REGION,
        client_factory: S3ClientFactory | None = None,
    ) -> None:
        self._endpoint: str = endpoint
        self._public_endpoint: str = public_endpoint
        self._bucket: str = bucket
        self._presign_ttl_seconds: int = presign_ttl_seconds
        self._chunk_bytes: int = chunk_bytes
        # @audit the credentials go into the closure below and live for the life of the process,
        # as botocore requires for signing. The demo's defaults are `minioadmin`/`minioadmin`
        # (`app/config.py`), a laptop credential and nothing more; a deployment supplies its own.
        # Nothing here logs either one, and neither is kept as an attribute where a repr would.
        self._factory: S3ClientFactory = client_factory or _client_factory(
            access_key=access_key, secret_key=secret_key, region=region
        )
        self._clients: dict[str, S3Client] = {}
        self._client_lock: threading.Lock = threading.Lock()
        self._bucket_ready: bool = False

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ensure_bucket(self) -> None:
        """Create `settings.object_store_bucket` when it is absent (D096).

        Called by `put` on every write, and callable by a composition root that would rather
        have the failure at start-up than on the first artifact. After one success it is a
        no-op, so the `head_bucket` costs one round trip per process rather than one per object.
        """
        await asyncio.to_thread(self._ensure_bucket_blocking)

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        """Store the bytes and return the uri the `artifacts` row will carry.

        `media_type` becomes the object's `ContentType`. Nothing reads it back: the download
        response takes its `Content-Type` from `artifacts.mime` (D072). It is set because a
        presigned url is fetched by a browser directly, and a browser believes the store.

        The key is checked before anything is sent, so a key that could name something outside
        the bucket costs no request and writes no object.
        """
        safe_key = ensure_safe_key(key)
        await self.ensure_bucket()
        await asyncio.to_thread(self._put_blocking, safe_key, data, media_type)
        return build_storage_uri(scheme=S3_SCHEME, bucket=self._bucket, key=safe_key)

    def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        """Stream the object in `OBJECT_CHUNK_BYTES` chunks.

        A uri from another scheme or another bucket is refused here, synchronously, as the
        filesystem backend refuses it.

        A *missing* object is refused on the first chunk rather than at this call, which is the
        one place this backend cannot match `FilesystemObjectStore`: asking S3 whether an object
        exists is a network round trip, and doing it in this synchronous frame would put a
        blocking call on the event loop, which is the worse of the two problems. The error is
        the same `missing_object` every backend raises. A caller that must answer before it
        starts a response awaits `stat` first, which is what that method is for.
        """
        return self._stream(storage_uri, self._key_of(storage_uri))

    async def stat(self, storage_uri: str) -> ObjectStat | None:
        """Size without reading. `None` when the object is absent."""
        key = self._key_of(storage_uri)
        size = await asyncio.to_thread(self._stat_blocking, key)
        if size is None:
            return None
        return ObjectStat(storage_uri=storage_uri, size_bytes=size)

    async def presigned_url(self, storage_uri: str, *, ttl_seconds: int | None = None) -> str:
        """A url the client fetches from the object store directly, valid for `ttl_seconds`.

        Why it exists: streaming an 80 MiB video back through `open` spends this process's
        memory and one of its connections for the length of the download, and an API that is
        also a file server falls over on the day somebody downloads four videos at once. A
        presigned url moves those bytes onto the store and leaves us serving a redirect.

        **It is signed against the public endpoint, never the internal one.** Inside compose the
        store answers to `http://storage:9000`, a name that resolves on the compose network and
        nowhere else, so a browser on the host cannot fetch it. A SigV4 signature covers the
        host, so this is not a string anybody may rewrite afterwards: it has to be signed
        against the host the client will actually use. That is the whole reason
        `object_store_public_endpoint` exists and the whole reason this class holds two clients.
        Get it wrong and every test still passes while every url is unreachable.

        @audit a presigned url is a bearer token. Whoever holds the string reads those bytes
        until it expires, with no principal check, no audit trail on the fetch, and no way to
        revoke it short of rotating the store's credentials. That is consistent with a demo
        which has no authentication at all (`docs/demo.md`, "Identity"), but it is a second
        place the missing auth shows up rather than the same one twice. The TTL is the only
        control: `object_store_presign_ttl_seconds` is it, and this url never goes in a log.
        """
        key = self._key_of(storage_uri)
        ttl = self._presign_ttl_seconds if ttl_seconds is None else ttl_seconds
        return await asyncio.to_thread(self._presign_blocking, key, ttl)

    async def _stream(self, storage_uri: StorageUri, key: str) -> AsyncIterator[bytes]:
        body = await asyncio.to_thread(self._open_blocking, storage_uri, key)
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, self._chunk_bytes)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    def _client_for(self, endpoint: str) -> S3Client:
        """The client for one endpoint, built on first use.

        Called only from inside `asyncio.to_thread`, because `boto3.client` reads service model
        JSON off the disk and that is blocking. The lock is not paranoia: a botocore client is
        safe to *use* from several threads and building two at once is not, and `to_thread` runs
        on a pool with more than one thread in it.
        """
        with self._client_lock:
            client = self._clients.get(endpoint)
            if client is None:
                client = self._factory(endpoint)
                self._clients[endpoint] = client
            return client

    def _ensure_bucket_blocking(self) -> None:
        if self._bucket_ready:
            return
        client = self._client_for(self._endpoint)
        try:
            client.head_bucket(Bucket=self._bucket)
        except ClientError as error:
            if not _absent(error):
                raise
            try:
                client.create_bucket(Bucket=self._bucket)
            except ClientError as race:
                if _error_code(race) not in _BUCKET_EXISTS_CODES:
                    raise
        self._bucket_ready = True

    def _put_blocking(self, key: str, data: bytes, media_type: str) -> None:
        client = self._client_for(self._endpoint)
        client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=media_type)

    def _open_blocking(self, storage_uri: StorageUri, key: str) -> ByteStream:
        client = self._client_for(self._endpoint)
        try:
            response = client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if _absent(error):
                raise missing_object(storage_uri) from error
            raise
        return cast(ByteStream, response["Body"])

    def _stat_blocking(self, key: str) -> int | None:
        client = self._client_for(self._endpoint)
        try:
            response = client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if _absent(error):
                return None
            raise
        return int(cast(int, response["ContentLength"]))

    def _presign_blocking(self, key: str, ttl_seconds: int) -> str:
        client = self._client_for(self._public_endpoint)
        return client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    def _key_of(self, storage_uri: StorageUri) -> str:
        return parse_storage_uri(storage_uri, scheme=S3_SCHEME, bucket=self._bucket)
