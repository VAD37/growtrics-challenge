"""Injection points. Everything behind a router arrives through here.

Four providers, all stubs. `main.py` is the composition root and overrides each one with an
adapter (`app.dependency_overrides`), which is what lets this lane be finished and tested
before orchestration, custody or storage exist -- and what keeps the import-linter contract
"api reaches no adapter" true: the API names ports, never implementations.

Also here: the identity header, and the two paging parameters. Both are edge concerns in the
strict sense -- they turn a string off the wire into a typed value or a `400`, and neither
decides anything.
"""

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Query, Request

from app.access.ports import PrincipalResolver
from app.api.ports import ArtifactService, DatabaseProbe, JobService
from app.config import Settings, settings
from app.domain.access import AccessScope
from app.domain.records import Cursor

USER_ID_HEADER: Final[str] = "X-User-Id"
"""D086. Identity sits at the credential position, never in the body, so a request still
cannot name whose job it is and swapping in `Authorization: Bearer` changes one function."""


# --------------------------------------------------------------------------- providers


def get_settings() -> Settings:
    """The one reader of the environment, handed to routers rather than imported by them."""
    return settings


def get_job_service() -> JobService:
    # @TODO the composition root overrides this with the orchestration use cases
    # (docs/plan/03-module-layout.md). A router that runs unwired fails loudly here rather
    # than serving something invented.
    raise NotImplementedError("main.py must override get_job_service")


def get_artifact_service() -> ArtifactService:
    # @TODO overridden with custody's reader (docs/plan/03-module-layout.md).
    raise NotImplementedError("main.py must override get_artifact_service")


def get_database_probe() -> DatabaseProbe:
    # @TODO overridden with the SQL probe (docs/demo.md, "Surface").
    raise NotImplementedError("main.py must override get_database_probe")


def get_principal_resolver() -> PrincipalResolver:
    # @TODO overridden with app.access.stub.DemoPrincipalResolver, which needs the
    # PrincipalRepository the storage lane owns (D086).
    raise NotImplementedError("main.py must override get_principal_resolver")


# --------------------------------------------------------------------------- identity


async def current_scope(
    request: Request,
    resolver: Annotated[PrincipalResolver, Depends(get_principal_resolver)],
) -> AccessScope:
    """Steps 1 and 2 of `plan/12-data-control.md`, in the only order they happen.

    @audit no authentication and no authorisation. The header is an unverified claim, an
    absent one becomes the configured default principal rather than a `401`, and the scope
    this returns enforces no ownership. See `app/access/stub.py` for what restoring the check
    costs.
    """
    principal = await resolver.resolve(request.headers.get(USER_ID_HEADER))
    return resolver.scope_for(principal)


# --------------------------------------------------------------------------- paging


@dataclass(frozen=True, slots=True)
class PageParams:
    """A decoded cursor and a validated limit. The two arguments every listing takes."""

    cursor: Cursor | None
    limit: int


def page_params(
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=settings.page_max_limit)] = settings.page_default_limit,
) -> PageParams:
    """Decode the cursor once, at the edge.

    A malformed cursor is `400 INVALID_REQUEST` naming the field, raised by `Cursor.decode`
    rather than here: the encoding is the domain's and so is the complaint about it. Over-range
    limits are rejected and never clamped (D080), which is why the bound is on the `Query` and
    not an `min()` in a repository.
    """
    return PageParams(cursor=None if cursor is None else Cursor.decode(cursor), limit=limit)


# --------------------------------------------------------------------------- aliases

ScopeDep = Annotated[AccessScope, Depends(current_scope)]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
ArtifactServiceDep = Annotated[ArtifactService, Depends(get_artifact_service)]
DatabaseProbeDep = Annotated[DatabaseProbe, Depends(get_database_probe)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PageDep = Annotated[PageParams, Depends(page_params)]
