"""The in-memory object store, held to the same contract as the one that writes files.

If this double were permissive about keys or about uris, every test that used it would pass for
a reason the real backend does not share. So the assertions here are deliberately the same ones
`objects_filesystem_test.py` makes, minus the parts that are about a disk.
"""

import pytest

from app.domain.errors import DomainError, ErrorCode
from app.storage.memory import MemoryObjectStore
from app.storage.objects import UnknownStorageUriError, UnsafeObjectKeyError

KEY = "artifacts/job_00000000000000000000000000/abc.mp4"


async def drain(store: MemoryObjectStore, storage_uri: str) -> bytes:
    return b"".join([chunk async for chunk in store.open(storage_uri)])


async def test_put_returns_a_uri_that_reads_back_the_same_bytes() -> None:
    store = MemoryObjectStore()

    storage_uri = await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    assert storage_uri == f"memory://artifacts/{KEY}"
    assert await drain(store, storage_uri) == b"lesson bytes"
    assert store.media_types[storage_uri] == "video/mp4"


async def test_reading_is_chunked_rather_than_one_lump() -> None:
    """The port streams because a video is not something to hold in memory twice."""
    store = MemoryObjectStore(chunk_bytes=4)
    storage_uri = await store.put(KEY, b"0123456789", media_type="video/mp4")

    chunks = [chunk async for chunk in store.open(storage_uri)]

    assert chunks == [b"0123", b"4567", b"89"]


async def test_stat_answers_without_reading() -> None:
    store = MemoryObjectStore()
    storage_uri = await store.put(KEY, b"0123456789", media_type="video/mp4")

    stat = await store.stat(storage_uri)

    assert stat is not None
    assert stat.size_bytes == 10
    assert await store.stat("memory://artifacts/artifacts/nothing.mp4") is None


async def test_a_missing_object_is_not_ready_rather_than_not_found() -> None:
    """The row exists and the client was told so; `404` would be a lie it acts on."""
    store = MemoryObjectStore()

    with pytest.raises(DomainError) as raised:
        store.open("memory://artifacts/artifacts/missing.mp4")

    assert raised.value.code is ErrorCode.ARTIFACT_NOT_READY


async def test_a_uri_from_another_bucket_is_refused() -> None:
    store = MemoryObjectStore(bucket="artifacts")
    await store.put(KEY, b"lesson bytes", media_type="video/mp4")

    with pytest.raises(UnknownStorageUriError):
        await store.stat(f"memory://somebody-else/{KEY}")
    with pytest.raises(UnknownStorageUriError):
        await store.stat(f"file://artifacts/{KEY}")


@pytest.mark.parametrize(
    "key",
    ["../escape.mp4", "/absolute.mp4", "artifacts\\windows.mp4", "C:/drive.mp4", ""],
)
async def test_an_unsafe_key_never_reaches_the_store(key: str) -> None:
    store = MemoryObjectStore()

    with pytest.raises(UnsafeObjectKeyError):
        await store.put(key, b"lesson bytes", media_type="video/mp4")

    assert len(store) == 0
