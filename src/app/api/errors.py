"""Every failure leaves through one envelope.

`{"error": {"code", "message", "details"}}` is frozen (`plan/13-mvp.md`). The message comes
from `ERROR_CATALOG` and is never written at a raise site (D062), so a raise carries a code and
machine-readable details and nothing a translator would have to chase. The status comes from
the same catalog, so "which code is which status" is one table rather than a habit.

Three things can fail before a router body runs, and all three are converted here rather than
left to FastAPI's defaults: a schema violation (`422` by default, `400 INVALID_REQUEST` here,
because the frozen contract says so), an unmatched route or method, and a `DomainError` raised
behind the seam.

@TODO there is no catch-all `Exception` handler, so an unhandled error is a bare `500` with no
envelope and no `X-Schema-Version`. The eight codes in `ERROR_CATALOG` have no member meaning
"we crashed", and inventing one at this layer is exactly what D062 forbids. Adding
`INTERNAL_ERROR` to `domain/errors.py` needs a decision line; until then the gap is loud rather
than papered over with a code that means something else.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas.common import ErrorBody, ErrorEnvelope
from app.domain.errors import ERROR_CATALOG, DomainError, ErrorCode

ENVELOPE_KEYS: Final[tuple[str, ...]] = ("schema_version", "error")
ERROR_BODY_KEYS: Final[tuple[str, ...]] = ("code", "message", "details")

INTERNAL_STATUS: Final[int] = 500
"""What a job-failure code becomes if it ever reaches the edge as a raised error."""

_DETAIL_MAX_CHARS: Final[int] = 200
"""Detail values are echoed back to whoever sent them. A cap keeps a hostile field name from
turning an error body into a payload delivery mechanism."""

_BOUND_KEYS: Final[tuple[str, ...]] = ("le", "ge", "lt", "gt", "max_length", "min_length")
"""Pydantic's `ctx` keys that name a bound, in the order the demo's fields use them."""

ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    400: {"model": ErrorEnvelope, "description": "INVALID_REQUEST"},
    404: {"model": ErrorEnvelope, "description": "JOB_NOT_FOUND, ARTIFACT_NOT_FOUND"},
    409: {"model": ErrorEnvelope, "description": "ARTIFACT_NOT_READY"},
    429: {"model": ErrorEnvelope, "description": "TOO_MANY_ACTIVE_JOBS"},
}
"""Declared on every `/v1` router so the generated OpenAPI shows the envelope rather than only
the happy path. FastAPI still advertises its own `422`; nothing here returns one, because
`validation_error_handler` converts every schema violation into a `400`."""


def http_status_for(code: ErrorCode) -> int:
    """The catalog's status, or `500` for a code that is not a rejected request.

    `GENERATION_FAILED` has `http_status=None`: it belongs in `failure.code` on a `200` job
    document, because the request to read a failed job did not fail. Reaching this function
    means something raised it at the edge, which is a server fault and not the client's.
    """
    return ERROR_CATALOG[code].http_status or INTERNAL_STATUS


def envelope_for(code: ErrorCode, details: Mapping[str, str] | None = None) -> ErrorEnvelope:
    return ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=ERROR_CATALOG[code].message,
            details=dict(details or {}),
        )
    )


def error_response(code: ErrorCode, details: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http_status_for(code),
        content=envelope_for(code, details).model_dump(mode="json", by_alias=True),
    )


def details_from_validation_error(errors: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Turn pydantic's report into the machine-readable half of the envelope.

    The first error only, plus a count. A client fixes one field at a time, and a full dump of
    every failure on a rejected body is a larger response than the body was. `field` is a
    dotted path so `body.options.max_duration_s` says which of two duration-shaped fields was
    meant, and `limit` names the bound rather than making the client re-read the docs (D080).
    """
    if not errors:
        return {}
    first = errors[0]
    location = first.get("loc")
    path = ".".join(str(part) for part in location) if isinstance(location, tuple) else ""
    details: dict[str, str] = {
        "field": path or "body",
        "rule": str(first.get("type", "")),
        "reason": str(first.get("msg", "")),
    }
    context = first.get("ctx")
    if isinstance(context, Mapping):
        for key in _BOUND_KEYS:
            if key in context:
                details["limit"] = str(context[key])
                break
    if len(errors) > 1:
        details["error_count"] = str(len(errors))
    return {key: value[:_DETAIL_MAX_CHARS] for key, value in details.items()}


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return error_response(exc.code, exc.details)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's `422` becomes the frozen `400 INVALID_REQUEST`.

    This is also where a body that is not JSON at all lands, because FastAPI reports a decode
    failure as a validation error rather than as an exception.
    """
    return error_response(ErrorCode.INVALID_REQUEST, details_from_validation_error(exc.errors()))


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Transport-level refusals: an unmatched path, a method that does not exist here.

    The status is preserved and the code is `INVALID_REQUEST`, which is honest about what
    happened without inventing a vocabulary member. @TODO a generic `NOT_FOUND` code would say
    it better and needs a decision line first (`docs/decisions.md`, D062 and D072).
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope_for(
            ErrorCode.INVALID_REQUEST, {"status": str(exc.status_code)}
        ).model_dump(mode="json", by_alias=True),
        headers=exc.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Called by `app.api.install`.

    The handlers are typed against the exception each one actually receives, which is narrower
    than starlette's `Callable[[Request, Exception], Response]`. That is the documented way to
    register one and the reason these three lines are the only place the variance shows.
    """
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
