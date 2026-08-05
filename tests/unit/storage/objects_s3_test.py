"""The deferred backend, held to the one thing a deferred backend must do: fail loudly.

D096 picks S3 over `boto3` and this file is where that lands, unbuilt. A stub that returned a
plausible `storage_uri` would put a row in the database pointing at bytes nobody wrote, and no
later sweep could tell that row from a real one. So every method raises, and these tests are
what stop somebody "finishing" one of them with a return value.
"""

import pytest

from app.storage.objects import S3_SCHEME, S3ObjectStore

STORAGE_URI = "s3://artifacts/artifacts/job_00000000000000000000000000/abc.mp4"


def a_store() -> S3ObjectStore:
    return S3ObjectStore(
        endpoint="http://storage:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        bucket="artifacts",
    )


def test_the_constructor_resolves_configuration_so_wiring_it_is_a_composition_change() -> None:
    assert a_store().bucket == "artifacts"
    assert S3_SCHEME == "s3"


async def test_ensure_bucket_refuses_rather_than_pretending() -> None:
    with pytest.raises(NotImplementedError):
        await a_store().ensure_bucket()


async def test_put_never_returns_a_uri_it_did_not_write() -> None:
    with pytest.raises(NotImplementedError):
        await a_store().put("artifacts/lesson.mp4", b"lesson bytes", media_type="video/mp4")


async def test_open_refuses_rather_than_streaming_nothing() -> None:
    with pytest.raises(NotImplementedError):
        a_store().open(STORAGE_URI)


async def test_stat_refuses_rather_than_reporting_absent() -> None:
    """`None` here would read as "the bytes are missing" rather than "this backend is unbuilt"."""
    with pytest.raises(NotImplementedError):
        await a_store().stat(STORAGE_URI)
