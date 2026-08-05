"""Trace ids: creation is real, propagation is a stub.

A trace id ties every line about one attempt together. Two ways to get one, and they answer
different questions:

* `trace_id_for_attempt(job_id, attempt)` is `app.domain.ids.derive_trace_id`, deterministic and
  stable per attempt (D072). The worker uses it, so a retry's lines do not merge into the
  previous try's trace, and so both sides of a seam can name the same trace without passing it.
* `new_trace_id()` mints one from entropy. The API edge uses it, because a request that has not
  been admitted yet has no job id to derive from.

Propagation is DEFERRED, STUB ONLY (scope override item 8). What the eventual implementation
does: a `contextvars.ContextVar[TraceId]` set once per request in an ASGI middleware and once
per claim in the worker loop, read by the log formatter, written to `job_events.trace_id`
(D093, still no table) and to `jobs.failure.trace_id` (`FailureRecord.trace_id`, which is a
real field today), and echoed back on the response as `X-Trace-Id`.

The consequence, said out loud: until it is filled in, a trace id reaches whatever it reaches by
being passed as an argument. Nothing correlates an API log line with the worker line for the
same job, and the only trace id a client ever sees is the one inside a failed job's `failure`
block.
"""

from typing import Final
from uuid import uuid4

from app.domain.ids import JobId, TraceId, derive_trace_id, encode_crockford

TRACE_PREFIX: Final[str] = "tr_"
"""Matches `app.domain.ids.TRACE_ID_PATTERN`. One prefix, written where ids are made."""

TRACE_HEADER: Final[str] = "X-Trace-Id"
"""Request and response header. Read as a hint and never trusted as an identity.

@audit inbound this is client-controlled text. When propagation is built, an inbound value is
validated with `is_trace_id` and otherwise replaced -- it is a correlation key, so an attacker
supplying one can at worst merge their own lines, and only if it is well formed.
"""


def mint_trace_id(entropy: bytes) -> TraceId:
    """Render sixteen bytes as a trace id. Pure, so a test can pin one.

    Same split as `app.domain.ids.mint_request_key`: the caller owns the entropy, and the one
    function that reads a random source is the wrapper below.
    """
    return f"{TRACE_PREFIX}{encode_crockford(entropy)}"


def new_trace_id() -> TraceId:
    """Mint a trace id for work that has no job id yet, such as an inbound request."""
    return mint_trace_id(uuid4().bytes)


def trace_id_for_attempt(job_id: JobId, attempt: int) -> TraceId:
    """The trace of one attempt at one job (D072). Delegates to the domain derivation.

    Here so that callers ask this package for a trace id rather than reaching into `domain.ids`
    for one, which is what keeps the propagation stub below the only thing to change later.
    """
    return derive_trace_id(job_id, attempt)


def bind_trace_id(trace_id: TraceId) -> None:
    """Make `trace_id` the current one for this task and everything it awaits."""
    # @TODO propagation (scope override item 8; docs/plan/13-mvp.md stage 6). Set a
    # `contextvars.ContextVar[TraceId]` here, from ASGI middleware per request and from the
    # worker loop per claim, and return its `Token` so the caller can reset it.
    raise NotImplementedError


def current_trace_id() -> TraceId:
    """The trace id bound to this task."""
    # @TODO propagation (scope override item 8). Reads the same ContextVar. Until then a trace
    # id travels as an argument, and the log formatter has nothing to attach.
    raise NotImplementedError
