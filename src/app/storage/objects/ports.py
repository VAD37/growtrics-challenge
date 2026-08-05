"""Where artifact bytes live, and the rules every backend of them obeys.

Bytes are not in the database (D052). The `artifacts` row holds the metadata and a
`storage_uri`; the object behind that uri is what a client eventually downloads, and the two are
written in that order by `custody.store.ArtifactPublisher`.

`ObjectStore` is a superset of `custody.ports.ArtifactStore`: the same `put` and the same `open`,
plus `stat`. Custody keeps the smaller port because writing and streaming is all it does; the
extra method is for whoever has to answer "the row says these bytes exist -- do they" without
reading 80 MiB to find out.

Three things are shared rather than reimplemented per backend, because getting any of them wrong
in one adapter and right in another is how a demo passes its tests and loses a file:

* `ensure_safe_key` decides what a key may look like. Keys are server-derived
  (`custody.store.object_key`), so a rejected key is a bug in this system rather than a client's
  input, which is why it raises `UnsafeObjectKeyError` rather than a `DomainError`.
* `build_storage_uri` and `parse_storage_uri` are one format, `{scheme}://{bucket}/{key}`, so a
  row written by one backend is legible to the next and a row naming somebody else's bucket is
  refused instead of opened.
* `missing_object` is the one failure a caller has to distinguish, and every backend raises the
  identical error for it.

`put` takes whole bytes rather than a stream: `custody.ports.ArtifactStore` is merged and
declares it that way, and custody has already hashed and verified the complete object by the
time it stores anything. The read side is where size actually matters, and that streams.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from app.domain.errors import DomainError, ErrorCode

__all__ = [
    "MAX_KEY_CHARS",
    "OBJECT_CHUNK_BYTES",
    "ObjectKey",
    "ObjectStat",
    "ObjectStore",
    "StorageUri",
    "UnknownStorageUriError",
    "UnsafeObjectKeyError",
    "build_storage_uri",
    "ensure_safe_key",
    "missing_object",
    "parse_storage_uri",
]

type ObjectKey = str
"""A path inside one bucket. Slash-separated, relative, and never a name of its own choosing."""

type StorageUri = str
"""`{scheme}://{bucket}/{key}`, as written to `artifacts.storage_uri`."""

OBJECT_CHUNK_BYTES: Final[int] = 64 * 1024
"""Read size for streaming. Small enough that a 64 MiB video never sits in memory twice."""

MAX_KEY_CHARS: Final[int] = 512
"""A derived key is around 60 characters. Anything longer did not come from `object_key`."""

_URI_SEPARATOR: Final[str] = "://"


class UnsafeObjectKeyError(ValueError):
    """A key that could name something outside its bucket. Always a bug on our side."""


class UnknownStorageUriError(ValueError):
    """A `storage_uri` this backend does not own: wrong scheme, wrong bucket, or malformed."""


@dataclass(frozen=True, slots=True)
class ObjectStat:
    """What a backend can say about an object without reading it.

    No media type. `artifacts.mime` is the truth (D072), and a store echoing its own guess would
    hand custody a second answer to reconcile against the row it already wrote.
    """

    storage_uri: StorageUri
    size_bytes: int


def missing_object(storage_uri: StorageUri) -> DomainError:
    """The row exists and its bytes do not.

    `ARTIFACT_NOT_READY` rather than `ARTIFACT_NOT_FOUND`: the artifact is real, the client was
    told it exists, and answering `404` would be a lie it acts on (`docs/demo.md`, "Errors").
    """
    return DomainError(ErrorCode.ARTIFACT_NOT_READY, {"reason": "object_missing"})


def ensure_safe_key(key: str) -> ObjectKey:
    """Return `key` if it names something inside a bucket, and refuse it otherwise.

    The refusals, in the order an attacker would try them: an absolute path, a `..` segment, a
    backslash, a drive letter, and a control character. A backslash is refused outright rather
    than normalised, because on Windows it is a separator and on POSIX it is a legal filename
    character, and one string must not mean two different files.

    @audit this is a string check and nothing more. A backend over a real filesystem must ALSO
    confirm the resolved path is inside its root, because a symlink planted in the bucket turns
    a perfectly safe key into a write anywhere on the disk.
    """
    if not key or len(key) > MAX_KEY_CHARS:
        raise UnsafeObjectKeyError(f"object key must be 1 to {MAX_KEY_CHARS} characters")
    if "\\" in key:
        raise UnsafeObjectKeyError("object key must not contain a backslash")
    if ":" in key:
        raise UnsafeObjectKeyError("object key must not contain a colon")
    if any(char < " " or char == "\x7f" for char in key):
        raise UnsafeObjectKeyError("object key must not contain a control character")
    segments = key.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise UnsafeObjectKeyError("object key must not contain an empty or relative segment")
    return key


def build_storage_uri(*, scheme: str, bucket: str, key: str) -> StorageUri:
    """The uri that goes on the row. The key is checked here so a bad one never reaches a row."""
    return f"{scheme}{_URI_SEPARATOR}{bucket}/{ensure_safe_key(key)}"


def parse_storage_uri(storage_uri: str, *, scheme: str, bucket: str) -> ObjectKey:
    """The key inside our own bucket, or a refusal.

    A uri naming another scheme or another bucket is refused rather than coerced. Rows outlive
    deployments: a database restored beside a differently configured store must fail to read
    that object rather than quietly read a same-named one out of the current bucket.
    """
    prefix = f"{scheme}{_URI_SEPARATOR}{bucket}/"
    if not storage_uri.startswith(prefix):
        raise UnknownStorageUriError(f"storage uri is not in {scheme}://{bucket}")
    return ensure_safe_key(storage_uri[len(prefix) :])


@runtime_checkable
class ObjectStore(Protocol):
    """The artifact bytes, behind one seam. Satisfies `custody.ports.ArtifactStore`."""

    async def put(self, key: str, data: bytes, *, media_type: str) -> str:
        """Store `data` under `key` and return the `storage_uri` for the row.

        `media_type` is recorded by a backend that has somewhere to record it (S3 keeps it as
        `ContentType`) and ignored by one that does not. Nothing reads it back: the response's
        `Content-Type` comes from `artifacts.mime`.
        """
        ...

    def open(self, storage_uri: str) -> AsyncIterator[bytes]:
        """Stream the object. Synchronous, returning an iterator, exactly as custody declares it.

        Raises the error `missing_object` builds when there is no such object, at call time
        rather than on the first chunk, so the caller learns before it has started a response.
        """
        ...

    async def stat(self, storage_uri: str) -> ObjectStat | None:
        """Size without reading. `None` when the object is absent."""
        ...
