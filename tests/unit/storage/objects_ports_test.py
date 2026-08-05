"""The rules every object backend shares: what a key may be, and what a uri may name.

The first test is the one that matters most. Keys are not client input -- `custody.store` derives
them -- so the useful question is not "does this reject junk" but "does it accept what custody
actually produces". A key rule that quietly refused a real key would fail on the first artifact
of a demo run and nowhere earlier.
"""

import pytest
from memory_rows_test import content_hash_for, job_id_for

from app.custody.store import object_key
from app.domain.enums import ScanVerdict
from app.domain.errors import ErrorCode
from app.storage.objects import (
    UnknownStorageUriError,
    UnsafeObjectKeyError,
    build_storage_uri,
    ensure_safe_key,
    missing_object,
    parse_storage_uri,
)


@pytest.mark.parametrize("verdict", list(ScanVerdict))
def test_the_keys_custody_derives_are_accepted(verdict: ScanVerdict) -> None:
    key = object_key(job_id_for(0), content_hash_for(0), "video/mp4", verdict)

    assert ensure_safe_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("../../etc/passwd", id="climbs out of the bucket"),
        pytest.param("artifacts/../../escape.mp4", id="climbs out from the middle"),
        pytest.param("/etc/passwd", id="absolute path names its own root"),
        pytest.param("artifacts\\job\\lesson.mp4", id="backslash is a separator on Windows"),
        pytest.param("C:/artifacts/lesson.mp4", id="drive letter is absolute too"),
        pytest.param("artifacts//lesson.mp4", id="empty segment"),
        pytest.param("artifacts/./lesson.mp4", id="dot segment is one key spelled twice"),
        pytest.param("artifacts/lesson.mp4/", id="trailing slash names a directory"),
        pytest.param("artifacts/lesson\n.mp4", id="control character"),
        pytest.param("", id="no key at all"),
        pytest.param("a" * 513, id="longer than anything object_key derives"),
    ],
)
def test_a_key_that_could_name_something_else_is_refused(key: str) -> None:
    with pytest.raises(UnsafeObjectKeyError):
        ensure_safe_key(key)


def test_a_uri_round_trips_through_its_own_scheme_and_bucket() -> None:
    key = "artifacts/job_x/lesson.mp4"

    storage_uri = build_storage_uri(scheme="file", bucket="artifacts", key=key)

    assert storage_uri == "file://artifacts/artifacts/job_x/lesson.mp4"
    assert parse_storage_uri(storage_uri, scheme="file", bucket="artifacts") == key


@pytest.mark.parametrize(
    "storage_uri",
    [
        "s3://artifacts/artifacts/lesson.mp4",
        "file://other-bucket/artifacts/lesson.mp4",
        "artifacts/lesson.mp4",
        "file://artifacts/../lesson.mp4",
    ],
)
def test_a_uri_this_backend_does_not_own_is_refused(storage_uri: str) -> None:
    """Rows outlive deployments: a restored database must fail rather than read the wrong file."""
    with pytest.raises((UnknownStorageUriError, UnsafeObjectKeyError)):
        parse_storage_uri(storage_uri, scheme="file", bucket="artifacts")


def test_a_missing_object_is_reported_as_not_ready() -> None:
    error = missing_object("file://artifacts/artifacts/lesson.mp4")

    assert error.code is ErrorCode.ARTIFACT_NOT_READY
    assert error.details == {"reason": "object_missing"}
