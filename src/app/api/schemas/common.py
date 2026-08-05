"""The shapes every response shares: the schema version, the page, the error envelope.

Three versions meet at this edge and they answer different questions (scope override item 7):

* the **path** version, `/v1`. Moves only when something is removed or changes type. A new
  path version is a new constant here and a client release (D072).
* `schema_version`, `MAJOR.MINOR`, on every top-level response document and on the error
  envelope, and repeated in the `X-Schema-Version` response header. Adding an optional field
  bumps the minor. That is the change a client is entitled to ignore, and the header lets one
  tell whether the field it wants exists without parsing a body.
* `jobs.contract_version` and `artifacts.validator_version`, which are about the job and the
  checks that ran on its output. They keep their existing meaning and never move with this one.

One constant, referenced everywhere, because a version string written twice will disagree with
itself. Nested models never repeat it: it describes the document, not the fields.
"""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from app.domain.errors import ErrorCode

SCHEMA_VERSION: Final[str] = "1.0"
"""The one place this string is written.

Bump the minor when a field is added to any response document. Removing a field or changing
its type is `/v2` and a new constant, never a major bump of this one, because a client that
pinned `/v1` must keep receiving `/v1`.
"""

SCHEMA_VERSION_HEADER: Final[str] = "X-Schema-Version"
"""Set on every response, including errors and `304`s. See `app/api/__init__.py`."""

SCHEMA_VERSION_PATTERN: Final[str] = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
"""`MAJOR.MINOR`, no build metadata, no pre-release. Tested against the constant."""


class NestedView(BaseModel):
    """Base for a model that appears inside a document. Carries no `schema_version`."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ResponseDocument(NestedView):
    """Base for a top-level response document. Every one of them carries the version."""

    schema_version: str = SCHEMA_VERSION


class PageView[ItemT](ResponseDocument):
    """Every list response, always an object and never a bare array (D072).

    A bare array cannot grow a `next_cursor` without breaking every parser that consumed it.
    The cursor is opaque and keyset, never an offset: offsets skip and duplicate rows under
    concurrent inserts, which a job list gets constantly.
    """

    items: list[ItemT]
    next_cursor: str | None = None


class ErrorBody(NestedView):
    """`message` comes from `ERROR_CATALOG` and is never written at a raise site (D062).

    `details` is the machine-readable half: which field, which bound. Strings only, so nothing
    can carry a stray object graph out of the process by accident.
    """

    code: ErrorCode
    message: str
    details: dict[str, str]


class ErrorEnvelope(ResponseDocument):
    """The frozen error shape: `{"error": {"code", "message", "details"}}` (`plan/13-mvp.md`)."""

    error: ErrorBody


class HealthView(ResponseDocument):
    """`GET /health`. A report rather than a rejection, so it is never an error envelope.

    The status code carries the answer as well as the body: `200` when the database is
    reachable, `503` when it is not, so a container health check needs no JSON parser.
    """

    status: Literal["ok", "degraded"]
    database: Literal["up", "down"]
    version: str
