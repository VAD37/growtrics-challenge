"""The API's start-up, when start-up does not finish.

`open_resources` opens the pool and then reaches for the bucket. The second step is the one that
can fail on a checkout where the object store is not up yet, and by then the pool is real: unless
the acquisition is itself inside the lifespan's `try`, the process exits holding connections that
Postgres will not reclaim until the socket dies.
"""

from dataclasses import dataclass, replace

import pytest
from fastapi import FastAPI

from app.composition import Adapters, build_adapters
from app.config import settings
from app.main import _lifespan_for


@dataclass(slots=True)
class RecordingEngine:
    """`SqlEngine`'s lifetime, counted. Opening is the step that succeeds before the bucket."""

    opened: int = 0
    closed: int = 0

    async def open(self) -> None:
        self.opened += 1

    async def close(self) -> None:
        self.closed += 1


@dataclass(slots=True)
class UnreachableBucket:
    """`S3ObjectStore`, as far as `open_resources` uses it: a bucket that will not answer."""

    bucket: str = "artifacts"

    async def ensure_bucket(self) -> None:
        raise RuntimeError("the object store is not there")


def adapters_whose_bucket_is_gone(engine: RecordingEngine) -> Adapters:
    """The real bag with the two things `open_resources` touches swapped for doubles."""
    return replace(
        build_adapters(settings),
        engine=engine,
        objects=UnreachableBucket(),
    )


async def test_a_bucket_that_fails_at_start_up_still_gives_the_pool_back() -> None:
    engine = RecordingEngine()
    lifespan = _lifespan_for(adapters_whose_bucket_is_gone(engine))

    with pytest.raises(RuntimeError):
        async with lifespan(FastAPI()):
            pass

    assert engine.opened == 1
    assert engine.closed == 1
