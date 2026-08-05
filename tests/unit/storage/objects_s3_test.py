"""The MinIO backend: what it signs, where it signs it, and what thread it runs on.

Three claims in this file are the ones worth having.

**The signing client is built against the public endpoint.** This is the bug the adapter exists
to not have. Inside compose the store is `http://storage:9000`, which resolves on the compose
network and nowhere else; a browser on the host has to be sent to `http://localhost:9000`. A
SigV4 signature covers the host, so a url signed against the internal name cannot be patched
into a working one afterwards. Every other test in this file passes whether or not that split is
right, which is exactly why it gets its own test against a real `boto3` client.

**Nothing blocking runs on the event loop.** `boto3` is synchronous and this adapter lives in
the API process. The fake client records the thread it was called on, and the tests assert that
thread is not the one running the test, so "it goes through `asyncio.to_thread`" is checked
rather than believed.

**A key that could name something outside the bucket costs no request.** The refusals are the
same ones `FilesystemObjectStore` makes, because they come from the same `ports.py` function.

The fake is a dict with S3's error codes bolted on, not a mock: an adapter tested against
`assert_called_with` passes while spelling `NoSuchKey` wrong. The live suite at the bottom runs
the same operations against a real MinIO when one is reachable, and skips loudly when it is not.
"""

import asyncio
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError

from app.config import settings
from app.domain.errors import DomainError, ErrorCode
from app.storage.objects import (
    S3_SCHEME,
    FilesystemObjectStore,
    ObjectStore,
    PresigningObjectStore,
    S3Client,
    S3ClientFactory,
    S3ObjectStore,
    UnknownStorageUriError,
    UnsafeObjectKeyError,
)

KEY = "artifacts/job_00000000000000000000000000/abc.mp4"
STORAGE_URI = f"s3://artifacts/{KEY}"

INTERNAL_ENDPOINT = "http://storage:9000"
PUBLIC_ENDPOINT = "http://localhost:9000"


# --- the fake store -----------------------------------------------------------------


def client_error(code: str, *, status: int, operation: str) -> ClientError:
    """A botocore failure spelled the way the real thing spells it."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": "not here"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeBody:
    """A `get_object` body. Reads in whatever size it is asked for, and remembers being closed."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0
        self.closed = False

    def read(self, amt: int) -> bytes:
        chunk = self._data[self._offset : self._offset + amt]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeS3:
    """One store's worth of state, shared by every client its factory hands out.

    Shared on purpose: the adapter builds two clients, one per endpoint, and an object written
    through the internal one has to be the object the public one signs for.
    """

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.content_types: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.endpoints: list[str] = []
        self.threads: set[int] = set()
        self.presigned: list[tuple[str, str, int]] = []
        """(endpoint, key, ExpiresIn) for every url handed out."""
        self.bodies: list[FakeBody] = []

    def factory(self, endpoint: str) -> S3Client:
        """Building a client is a blocking call too, so where it happens is recorded as well."""
        self.endpoints.append(endpoint)
        self.threads.add(threading.get_ident())
        return FakeClient(self, endpoint)

    def record(self, name: str) -> None:
        self.calls.append(name)
        self.threads.add(threading.get_ident())


class FakeClient:
    """The five calls the adapter makes, over a dict, raising S3's codes on absence."""

    def __init__(self, fake: FakeS3, endpoint: str) -> None:
        self._fake = fake
        self._endpoint = endpoint

    def head_bucket(self, *, Bucket: str) -> Mapping[str, object]:
        self._fake.record("head_bucket")
        if Bucket not in self._fake.buckets:
            raise client_error("404", status=404, operation="HeadBucket")
        return {}

    def create_bucket(self, *, Bucket: str) -> Mapping[str, object]:
        self._fake.record("create_bucket")
        self._fake.buckets.add(Bucket)
        return {}

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> Mapping[str, object]:
        self._fake.record("put_object")
        self._fake.objects[(Bucket, Key)] = Body
        self._fake.content_types[(Bucket, Key)] = ContentType
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        self._fake.record("get_object")
        data = self._fake.objects.get((Bucket, Key))
        if data is None:
            raise client_error("NoSuchKey", status=404, operation="GetObject")
        body = FakeBody(data)
        self._fake.bodies.append(body)
        return {"Body": body}

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        self._fake.record("head_object")
        data = self._fake.objects.get((Bucket, Key))
        if data is None:
            raise client_error("404", status=404, operation="HeadObject")
        return {"ContentLength": len(data)}

    def generate_presigned_url(
        self, *, ClientMethod: str, Params: Mapping[str, str], ExpiresIn: int
    ) -> str:
        self._fake.record("generate_presigned_url")
        self._fake.presigned.append((self._endpoint, Params["Key"], ExpiresIn))
        return f"{self._endpoint}/{Params['Bucket']}/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


def factory_of(client: S3Client) -> S3ClientFactory:
    """One client for every endpoint. For the tests that want a call to fail a specific way."""

    def build(endpoint: str) -> S3Client:
        return client

    return build


def a_store(fake: FakeS3, **overrides: object) -> S3ObjectStore:
    arguments: dict[str, object] = {
        "endpoint": INTERNAL_ENDPOINT,
        "public_endpoint": PUBLIC_ENDPOINT,
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
        "bucket": "artifacts",
        "client_factory": fake.factory,
    }
    arguments.update(overrides)
    return S3ObjectStore(**arguments)  # type: ignore[arg-type]


async def drain(store: S3ObjectStore, storage_uri: str) -> bytes:
    return b"".join([chunk async for chunk in store.open(storage_uri)])


# --- the two clients ----------------------------------------------------------------


async def test_a_presigned_url_is_signed_against_the_public_endpoint_not_the_internal_one() -> None:
    """The one test that fails when the two clients get confused.

    Real `boto3`, no server: signing is arithmetic over the credentials and the host, so this
    needs nothing running. `storage:9000` in the result would mean every url the demo hands a
    browser resolves nowhere, while every other test in this file still passed.
    """
    store = S3ObjectStore(
        endpoint=INTERNAL_ENDPOINT,
        public_endpoint=PUBLIC_ENDPOINT,
        access_key="minioadmin",
        secret_key="minioadmin",
        bucket="artifacts",
    )

    url = await store.presigned_url(STORAGE_URI)

    assert url.startswith(f"{PUBLIC_ENDPOINT}/artifacts/{KEY}?")
    assert "storage:9000" not in url
    assert urlparse(url).netloc == "localhost:9000"


async def test_a_presigned_url_carries_a_signature_and_its_own_expiry() -> None:
    """SigV4, path addressing, and the TTL inside the signed query rather than beside it."""
    store = S3ObjectStore(
        endpoint=INTERNAL_ENDPOINT,
        public_endpoint=PUBLIC_ENDPOINT,
        access_key="minioadmin",
        secret_key="minioadmin",
        bucket="artifacts",
        presign_ttl_seconds=120,
    )

    query = parse_qs(urlparse(await store.presigned_url(STORAGE_URI)).query)

    assert query["X-Amz-Expires"] == ["120"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Signature"]


async def test_reading_and_writing_use_the_internal_endpoint() -> None:
    """The split runs both ways: the public endpoint signs and never carries a byte."""
    fake = FakeS3()
    store = a_store(fake)

    await store.put(KEY, b"lesson bytes", media_type="video/mp4")
    await drain(store, STORAGE_URI)
    await store.stat(STORAGE_URI)

    assert set(fake.endpoints) == {INTERNAL_ENDPOINT}

    await store.presigned_url(STORAGE_URI)

    assert fake.endpoints.count(PUBLIC_ENDPOINT) == 1
    assert [endpoint for endpoint, _, _ in fake.presigned] == [PUBLIC_ENDPOINT]


async def test_one_client_is_built_per_endpoint_and_then_reused() -> None:
    fake = FakeS3()
    store = a_store(fake)

    for _ in range(3):
        await store.put(KEY, b"lesson bytes", media_type="video/mp4")
        await store.presigned_url(STORAGE_URI)

    assert sorted(fake.endpoints) == sorted([INTERNAL_ENDPOINT, PUBLIC_ENDPOINT])


# --- nothing blocking on the event loop ---------------------------------------------


async def test_every_boto3_call_is_dispatched_off_the_event_loop() -> None:
    """Asserted, not assumed. One blocking S3 call here stalls every other request.

    The fake records `threading.get_ident()` on every call it receives, including the one that
    builds a client -- `boto3.client` reads service models off the disk and blocks too.
    """
    running_loop_thread = threading.get_ident()
    fake = FakeS3()
    store = a_store(fake)

    await store.ensure_bucket()
    await store.put(KEY, b"lesson bytes", media_type="video/mp4")
    await drain(store, STORAGE_URI)
    await store.stat(STORAGE_URI)
    await store.presigned_url(STORAGE_URI)

    assert set(fake.calls) == {
        "head_bucket",
        "create_bucket",
        "put_object",
        "get_object",
        "head_object",
        "generate_presigned_url",
    }
    assert running_loop_thread not in fake.threads


async def test_the_stream_reads_off_the_loop_as_well() -> None:
    """The chunk loop is where an 80 MiB download would otherwise block for its whole length."""
    running_loop_thread = threading.get_ident()
    fake = FakeS3()
    store = a_store(fake, chunk_bytes=4)
    await store.put(KEY, b"0123456789", media_type="video/mp4")
    fake.threads.clear()

    chunks = [chunk async for chunk in store.open(STORAGE_URI)]

    assert chunks == [b"0123", b"4567", b"89"]
    assert running_loop_thread not in fake.threads


# --- the bucket ---------------------------------------------------------------------


async def test_put_creates_the_bucket_when_it_is_absent() -> None:
    """Nothing in compose creates it and no fifth service exists to (D096)."""
    fake = FakeS3()
    store = a_store(fake)

    storage_uri = await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert fake.buckets == {"artifacts"}
    assert fake.objects[("artifacts", KEY)] == b"lesson bytes"
    assert storage_uri == STORAGE_URI


async def test_an_existing_bucket_is_left_alone() -> None:
    fake = FakeS3()
    fake.buckets.add("artifacts")
    store = a_store(fake)

    await store.ensure_bucket()

    assert fake.calls == ["head_bucket"]


async def test_the_bucket_is_checked_once_per_process_not_once_per_write() -> None:
    """`put` calls `ensure_bucket` every time, so the remembering is what keeps it one trip."""
    fake = FakeS3()
    store = a_store(fake)

    for _ in range(4):
        await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert fake.calls.count("head_bucket") == 1
    assert fake.calls.count("create_bucket") == 1
    assert fake.calls.count("put_object") == 4


async def test_losing_the_race_to_create_the_bucket_is_not_a_failure() -> None:
    """Two processes start together; the one that gets `BucketAlreadyOwnedByYou` still won."""
    fake = FakeS3()

    class RacedClient(FakeClient):
        def create_bucket(self, *, Bucket: str) -> Mapping[str, object]:
            fake.record("create_bucket")
            raise client_error("BucketAlreadyOwnedByYou", status=409, operation="CreateBucket")

    store = a_store(fake, client_factory=factory_of(RacedClient(fake, INTERNAL_ENDPOINT)))

    await store.ensure_bucket()

    assert fake.calls == ["head_bucket", "create_bucket"]


async def test_a_store_that_is_unhappy_for_another_reason_is_not_treated_as_absence() -> None:
    """A 403 means the credential is wrong. Creating a bucket over it would hide that."""
    fake = FakeS3()

    class RefusingClient(FakeClient):
        def head_bucket(self, *, Bucket: str) -> Mapping[str, object]:
            fake.record("head_bucket")
            raise client_error("AccessDenied", status=403, operation="HeadBucket")

    store = a_store(fake, client_factory=factory_of(RefusingClient(fake, INTERNAL_ENDPOINT)))

    with pytest.raises(ClientError):
        await store.ensure_bucket()

    assert "create_bucket" not in fake.calls


# --- reading ------------------------------------------------------------------------


async def test_reading_streams_in_chunks_rather_than_whole() -> None:
    fake = FakeS3()
    store = a_store(fake, chunk_bytes=4)
    await store.put(KEY, b"0123456789", media_type="video/mp4")

    chunks = [chunk async for chunk in store.open(STORAGE_URI)]

    assert chunks == [b"0123", b"4567", b"89"]
    assert await drain(store, STORAGE_URI) == b"0123456789"


async def test_the_body_is_closed_when_the_stream_ends() -> None:
    fake = FakeS3()
    store = a_store(fake, chunk_bytes=4)
    await store.put(KEY, b"0123456789", media_type="video/mp4")

    await drain(store, STORAGE_URI)

    assert [body.closed for body in fake.bodies] == [True]


async def test_the_body_is_closed_when_the_caller_walks_away_mid_download() -> None:
    """A client that hangs up must not leave a connection held open for the pool's lifetime."""
    fake = FakeS3()
    store = a_store(fake, chunk_bytes=4)
    await store.put(KEY, b"0123456789", media_type="video/mp4")

    stream = store.open(STORAGE_URI)
    assert await anext(stream) == b"0123"
    await stream.aclose()

    assert [body.closed for body in fake.bodies] == [True]


async def test_a_missing_object_is_reported_as_not_ready_and_not_as_a_botocore_error() -> None:
    """`ARTIFACT_NOT_READY`: the row says the artifact exists, so `404` would be a lie (D072)."""
    fake = FakeS3()
    store = a_store(fake)

    with pytest.raises(DomainError) as raised:
        await drain(store, STORAGE_URI)

    assert raised.value.code is ErrorCode.ARTIFACT_NOT_READY
    assert raised.value.details == {"reason": "object_missing"}


async def test_stat_reports_the_size_without_reading_the_object() -> None:
    fake = FakeS3()
    store = a_store(fake)
    await store.put(KEY, b"0123456789", media_type="video/mp4")

    stat = await store.stat(STORAGE_URI)

    assert stat is not None
    assert stat.size_bytes == 10
    assert stat.storage_uri == STORAGE_URI
    assert "get_object" not in fake.calls


async def test_stat_on_a_missing_object_is_none() -> None:
    fake = FakeS3()
    store = a_store(fake)

    assert await store.stat(STORAGE_URI) is None


async def test_put_records_the_media_type_because_a_browser_believes_the_store() -> None:
    """A presigned url is fetched by the browser directly, so nothing of ours sets the header."""
    fake = FakeS3()
    store = a_store(fake)

    await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert fake.content_types[("artifacts", KEY)] == "video/mp4"


# --- keys and uris ------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("../escape.mp4", id="dot dot"),
        pytest.param("artifacts/../../escape.mp4", id="climbs out from the middle"),
        pytest.param("/etc/passwd", id="absolute path"),
        pytest.param("artifacts\\escape.mp4", id="backslash"),
        pytest.param("C:/artifacts/lesson.mp4", id="drive letter"),
        pytest.param("", id="no key at all"),
    ],
)
async def test_a_key_that_escapes_the_bucket_sends_no_request(key: str) -> None:
    """One key policy, `ports.ensure_safe_key`, and it runs before anything leaves the process."""
    fake = FakeS3()
    store = a_store(fake)

    with pytest.raises(UnsafeObjectKeyError):
        await store.put(key, b"lesson bytes", media_type="video/mp4")

    assert fake.calls == []
    assert fake.objects == {}


@pytest.mark.parametrize(
    "storage_uri",
    [
        pytest.param(f"s3://somebody-else/{KEY}", id="another bucket"),
        pytest.param(f"file://artifacts/{KEY}", id="another scheme"),
        pytest.param(f"memory://artifacts/{KEY}", id="a test double's scheme"),
        pytest.param(KEY, id="no scheme at all"),
    ],
)
async def test_a_uri_this_backend_does_not_own_is_refused_everywhere(storage_uri: str) -> None:
    """A database restored beside a differently configured store must fail, not read the wrong
    object out of the current bucket."""
    fake = FakeS3()
    store = a_store(fake)

    with pytest.raises((UnknownStorageUriError, UnsafeObjectKeyError)):
        store.open(storage_uri)
    with pytest.raises((UnknownStorageUriError, UnsafeObjectKeyError)):
        await store.stat(storage_uri)
    with pytest.raises((UnknownStorageUriError, UnsafeObjectKeyError)):
        await store.presigned_url(storage_uri)

    assert fake.calls == []


async def test_a_foreign_uri_is_refused_before_the_stream_starts() -> None:
    """`open` is synchronous, so this one refusal does happen at the call site."""
    fake = FakeS3()
    store = a_store(fake)

    with pytest.raises(UnknownStorageUriError):
        store.open(f"s3://somebody-else/{KEY}")


# --- the ttl ------------------------------------------------------------------------


async def test_the_default_ttl_comes_from_settings_and_not_from_a_literal() -> None:
    fake = FakeS3()
    store = S3ObjectStore(
        endpoint=INTERNAL_ENDPOINT,
        public_endpoint=PUBLIC_ENDPOINT,
        bucket="artifacts",
        client_factory=fake.factory,
    )

    await store.presigned_url(STORAGE_URI)

    assert [ttl for _, _, ttl in fake.presigned] == [settings.object_store_presign_ttl_seconds]


async def test_a_configured_ttl_is_what_reaches_the_signature() -> None:
    fake = FakeS3()
    store = a_store(fake, presign_ttl_seconds=45)

    await store.presigned_url(STORAGE_URI)

    assert [ttl for _, _, ttl in fake.presigned] == [45]


async def test_a_caller_may_ask_for_a_shorter_url_than_the_default() -> None:
    fake = FakeS3()
    store = a_store(fake, presign_ttl_seconds=300)

    await store.presigned_url(STORAGE_URI, ttl_seconds=10)

    assert [ttl for _, _, ttl in fake.presigned] == [10]


async def test_the_url_names_the_object_the_uri_named() -> None:
    fake = FakeS3()
    store = a_store(fake)

    await store.presigned_url(STORAGE_URI)

    assert [key for _, key, _ in fake.presigned] == [KEY]


# --- the two protocols --------------------------------------------------------------


def test_the_s3_store_satisfies_both_protocols() -> None:
    store = a_store(FakeS3())

    assert isinstance(store, ObjectStore)
    assert isinstance(store, PresigningObjectStore)
    assert S3_SCHEME == "s3"


def test_the_filesystem_store_is_an_object_store_and_not_a_presigning_one(tmp_path: Path) -> None:
    """Why presigning is a second protocol: a directory cannot answer the question at all, so it
    does not have the method and `isinstance` says so rather than a raise saying it later."""
    store = FilesystemObjectStore(tmp_path, bucket="artifacts")

    assert isinstance(store, ObjectStore)
    assert not isinstance(store, PresigningObjectStore)


# --- live, against a real MinIO -----------------------------------------------------
#
# Skipped when nothing is listening, never failed and never passed silently: a green run that
# quietly did no I/O would be the same result as a green run that did, which is how a broken
# adapter ships. `make up` or `docker run -p 9000:9000 minio/minio server /data` gets these to
# run; `pytest -q` prints the skip count either way.

LIVE_ENDPOINT = os.environ.get("APP_TEST_MINIO_ENDPOINT", "http://127.0.0.1:9000")
LIVE_ACCESS_KEY = os.environ.get("APP_TEST_MINIO_ACCESS_KEY", "minioadmin")
LIVE_SECRET_KEY = os.environ.get("APP_TEST_MINIO_SECRET_KEY", "minioadmin")
LIVE_BUCKET = "objects-s3-test"


def minio_is_reachable(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/minio/health/live", timeout=2) as response:
            return response.status == 200
    except OSError, ValueError:
        return False


live = pytest.mark.skipif(
    not minio_is_reachable(LIVE_ENDPOINT),
    reason=f"no MinIO at {LIVE_ENDPOINT}; set APP_TEST_MINIO_ENDPOINT or run `make up`",
)


def a_live_store(**overrides: object) -> S3ObjectStore:
    arguments: dict[str, object] = {
        "endpoint": LIVE_ENDPOINT,
        "public_endpoint": LIVE_ENDPOINT,
        "access_key": LIVE_ACCESS_KEY,
        "secret_key": LIVE_SECRET_KEY,
        "bucket": LIVE_BUCKET,
    }
    arguments.update(overrides)
    return S3ObjectStore(**arguments)  # type: ignore[arg-type]


def live_key(name: str) -> str:
    """A key per test, so two runs against one MinIO do not read each other's bytes."""
    return f"artifacts/job_{os.getpid():026d}/{name}"


def fetch(url: str) -> tuple[int, str, bytes]:
    """Fetch a url the way a browser would: from outside this process, with no credentials.

    Synchronous on purpose, and reached through `asyncio.to_thread` from the async tests, which
    is the same discipline the adapter itself keeps.
    """
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read()


@live
async def test_live_put_open_and_stat_against_a_real_minio() -> None:
    store = a_live_store(chunk_bytes=4)
    key = live_key("round-trip.mp4")

    storage_uri = await store.put(key, b"0123456789", media_type="video/mp4")

    assert storage_uri == f"s3://{LIVE_BUCKET}/{key}"
    stat = await store.stat(storage_uri)
    assert stat is not None
    assert stat.size_bytes == 10
    assert [chunk async for chunk in store.open(storage_uri)] == [b"0123", b"4567", b"89"]


@live
async def test_live_a_presigned_url_is_fetchable_and_returns_the_bytes() -> None:
    """The end of the exercise: a url signed here, fetched by something that is not this
    process, returning the object and the media type the store recorded."""
    store = a_live_store()
    key = live_key("presigned.mp4")
    storage_uri = await store.put(key, b"lesson bytes", media_type="video/mp4")

    url = await store.presigned_url(storage_uri, ttl_seconds=60)

    assert urlparse(url).netloc == urlparse(LIVE_ENDPOINT).netloc
    status, content_type, body = await asyncio.to_thread(fetch, url)

    assert status == 200
    assert content_type == "video/mp4"
    assert body == b"lesson bytes"


@live
async def test_live_a_url_the_store_will_not_honour_is_refused() -> None:
    """The TTL is the only control on a bearer token, so MinIO had better be the one enforcing
    it. Tampering with the expiry breaks the signature, which is the same refusal an expired url
    gets and the one that proves the query string is signed rather than decorative."""
    store = a_live_store()
    key = live_key("tampered.mp4")
    storage_uri = await store.put(key, b"lesson bytes", media_type="video/mp4")

    url = await store.presigned_url(storage_uri, ttl_seconds=1)
    tampered = url.replace("X-Amz-Expires=1&", "X-Amz-Expires=86400&")

    with pytest.raises(urllib.error.HTTPError) as raised:
        await asyncio.to_thread(fetch, tampered)
    assert raised.value.code in (400, 403)


@live
async def test_live_a_missing_object_is_not_ready_rather_than_a_botocore_error() -> None:
    store = a_live_store()
    storage_uri = f"s3://{LIVE_BUCKET}/{live_key('never-written.mp4')}"
    await store.ensure_bucket()

    assert await store.stat(storage_uri) is None
    with pytest.raises(DomainError) as raised:
        [chunk async for chunk in store.open(storage_uri)]
    assert raised.value.code is ErrorCode.ARTIFACT_NOT_READY
