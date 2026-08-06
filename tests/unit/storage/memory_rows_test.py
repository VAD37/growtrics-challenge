"""What is left in this directory once the behaviour moved to `tests/contract/`.

The row builders and `assert_conforms` live in `tests/support/rows.py`, because the contract
suite and these tests build the same rows and a second `make_job` is a second definition of what
a job is. They are re-exported here: the sibling modules import them from this name, and this
module is what the storage lane's unit tests have always reached for.

The two tests below are the ones with nothing to compare against a second backend. The object
stores have no port pair in `tests/contract/` yet, and `assert_conforms` has to be able to fail
or the conformance test in the contract suite proves nothing.
"""

from pathlib import Path

from support.rows import (
    LEASE_SECONDS,
    OTHER_OWNER,
    OTHER_PRINCIPAL,
    OWNER,
    PRINCIPAL,
    T0,
    artifact_id_for,
    assert_conforms,
    brief_id_for,
    constraints,
    content_hash_for,
    demo_scope,
    job_id_for,
    make_artifact,
    make_brief,
    make_job,
    make_queued_item,
    make_request,
    make_submission,
    request_key_for,
    work_item_id_for,
)

from app.custody.ports import ArtifactStore
from app.orchestration.ports import JobRepository
from app.storage.objects import FilesystemObjectStore, ObjectStore, S3ObjectStore

__all__ = [
    "LEASE_SECONDS",
    "OTHER_OWNER",
    "OTHER_PRINCIPAL",
    "OWNER",
    "PRINCIPAL",
    "T0",
    "artifact_id_for",
    "assert_conforms",
    "brief_id_for",
    "constraints",
    "content_hash_for",
    "demo_scope",
    "job_id_for",
    "make_artifact",
    "make_brief",
    "make_job",
    "make_queued_item",
    "make_request",
    "make_submission",
    "request_key_for",
    "work_item_id_for",
]


def test_both_object_backends_conform_to_the_same_two_ports() -> None:
    """The stub is held to the port as strictly as the backend that works.

    A declared stub whose signatures had drifted would be discovered on the day somebody filled
    in its bodies, which is the worst possible day to discover it.
    """
    filesystem = FilesystemObjectStore(Path("unused"), bucket="artifacts")
    s3 = S3ObjectStore(bucket="artifacts")

    for store in (filesystem, s3):
        assert_conforms(store, ObjectStore)
        assert_conforms(store, ArtifactStore)


def test_a_double_missing_a_method_is_caught() -> None:
    """The conformance helper has to be able to fail, or it proves nothing."""

    class Hollow:
        pass

    try:
        assert_conforms(Hollow(), JobRepository)
    except AssertionError:
        return
    raise AssertionError("assert_conforms accepted a class with no methods")
