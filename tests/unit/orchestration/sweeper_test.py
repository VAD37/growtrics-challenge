"""The sweeper is a stub, and this is the one test that says so out loud.

Scope override item 8 defers everything that reacts to a lease running out. The consequence is
written in `sweeper.py` and repeated here so it cannot be forgotten in a diff: until this exists,
a worker that dies mid-job leaves its `work_items` row claimed forever and its job sits at
`RUNNING` until an operator looks.
"""

import pytest
from fakes_test import FakeJobRepository, FakeWorkQueue, FrozenClock

from app.orchestration.engine import sweeper


async def test_every_sweeper_entry_point_is_still_a_stub() -> None:
    clock = FrozenClock()
    with pytest.raises(NotImplementedError):
        await sweeper.reclaim_expired_leases(FakeWorkQueue(), clock)
    with pytest.raises(NotImplementedError):
        await sweeper.fail_stale_jobs(FakeJobRepository(), clock)
    with pytest.raises(NotImplementedError):
        await sweeper.run_sweeper(FakeWorkQueue(), FakeJobRepository(), clock)


def test_the_deferred_work_names_what_it_needs() -> None:
    """A stub without a docstring is a hole. This one has to name the table and the decision."""
    for entry in (sweeper.reclaim_expired_leases, sweeper.fail_stale_jobs, sweeper.run_sweeper):
        assert entry.__doc__ is not None
        assert "@TODO" in entry.__doc__
    assert sweeper.__doc__ is not None
    assert "work_items" in sweeper.__doc__
    assert "D093" in sweeper.__doc__
