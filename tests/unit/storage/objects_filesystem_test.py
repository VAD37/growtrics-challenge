"""The backend that actually writes files, tested against a real directory.

`tmp_path` rather than a mock: this is the store the demo runs on, and the two things most
worth knowing about it -- that it creates its own bucket, and that a written object is whole --
are only observable on a disk.
"""

from pathlib import Path

import pytest

from app.domain.errors import DomainError, ErrorCode
from app.storage.objects import (
    FilesystemObjectStore,
    UnknownStorageUriError,
    UnsafeObjectKeyError,
)

KEY = "artifacts/job_00000000000000000000000000/abc.mp4"


def store_at(root: Path, *, chunk_bytes: int = 64 * 1024) -> FilesystemObjectStore:
    return FilesystemObjectStore(root, bucket="artifacts", chunk_bytes=chunk_bytes)


def names_under(root: Path, pattern: str) -> list[str]:
    """Walk the tree from a synchronous frame; a directory walk has no place in a coroutine."""
    return sorted(path.name for path in root.rglob(pattern))


async def drain(store: FilesystemObjectStore, storage_uri: str) -> bytes:
    return b"".join([chunk async for chunk in store.open(storage_uri)])


async def test_put_creates_the_bucket_and_writes_the_object(tmp_path: Path) -> None:
    """Nothing in compose creates the bucket, so the first write has to (D096)."""
    store = store_at(tmp_path)
    assert not store.bucket_root.exists()

    storage_uri = await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert store.bucket_root.is_dir()
    assert storage_uri == f"file://artifacts/{KEY}"
    assert (store.bucket_root / KEY).read_bytes() == b"lesson bytes"


async def test_ensure_bucket_is_idempotent(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    await store.ensure_bucket()
    await store.ensure_bucket()

    assert store.bucket_root.is_dir()


async def test_a_written_object_is_whole_or_absent(tmp_path: Path) -> None:
    """The rename is what publishes the bytes; no half-written neighbour survives it."""
    store = store_at(tmp_path)

    await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert names_under(store.bucket_root, "*.part") == []
    assert names_under(store.bucket_root, "*.mp4") == ["abc.mp4"]


async def test_rewriting_the_same_key_keeps_one_object(tmp_path: Path) -> None:
    """Keys are content-addressed, so a repeat write is the same bytes arriving twice."""
    store = store_at(tmp_path)

    first = await store.put(KEY, b"lesson bytes", media_type="video/mp4")
    second = await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert first == second
    assert names_under(store.bucket_root, "*.mp4") == ["abc.mp4"]


async def test_reading_streams_in_chunks(tmp_path: Path) -> None:
    store = store_at(tmp_path, chunk_bytes=4)
    storage_uri = await store.put(KEY, b"0123456789", media_type="video/mp4")

    chunks = [chunk async for chunk in store.open(storage_uri)]

    assert chunks == [b"0123", b"4567", b"89"]
    assert await drain(store, storage_uri) == b"0123456789"


async def test_stat_reports_the_size_without_reading(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    storage_uri = await store.put(KEY, b"0123456789", media_type="video/mp4")

    stat = await store.stat(storage_uri)

    assert stat is not None
    assert stat.size_bytes == 10
    assert stat.storage_uri == storage_uri


async def test_stat_on_a_missing_object_is_none(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    assert await store.stat("file://artifacts/artifacts/missing.mp4") is None


async def test_a_missing_object_is_refused_before_the_response_starts(tmp_path: Path) -> None:
    """`open` raises at the call, not on the first chunk, so the caller can still answer 409."""
    store = store_at(tmp_path)

    with pytest.raises(DomainError) as raised:
        store.open("file://artifacts/artifacts/missing.mp4")

    assert raised.value.code is ErrorCode.ARTIFACT_NOT_READY


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("../escape.mp4", id="dot dot"),
        pytest.param("/etc/passwd", id="absolute path"),
        pytest.param("artifacts\\escape.mp4", id="backslash"),
    ],
)
async def test_a_key_that_escapes_the_root_writes_nothing(tmp_path: Path, key: str) -> None:
    store = store_at(tmp_path)

    with pytest.raises(UnsafeObjectKeyError):
        await store.put(key, b"lesson bytes", media_type="video/mp4")

    assert names_under(tmp_path, "*.mp4") == []


async def test_a_uri_from_another_bucket_is_refused(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    with pytest.raises(UnknownStorageUriError):
        store.open(f"file://somebody-else/{KEY}")
    with pytest.raises(UnknownStorageUriError):
        await store.stat(f"s3://artifacts/{KEY}")


async def test_two_stores_under_one_root_do_not_see_each_other(tmp_path: Path) -> None:
    """The bucket is a directory, so it is also the boundary between two of them."""
    mine = FilesystemObjectStore(tmp_path, bucket="artifacts")
    theirs = FilesystemObjectStore(tmp_path, bucket="quarantine")

    storage_uri = await mine.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert await theirs.stat(storage_uri.replace("artifacts", "quarantine", 1)) is None
