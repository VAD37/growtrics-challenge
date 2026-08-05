"""Time, held still.

`Clock` is a port for one reason: a test that asserts on `updated_at`, on a lease deadline, or
on the order of two rows has to be able to say what time it is. `FrozenClock` is the double the
rest of this package takes, so a job's timestamps come from the test rather than from whenever
the suite happened to run.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

__all__ = ["FrozenClock"]


@dataclass(slots=True)
class FrozenClock:
    """A `Clock` that only moves when a test says so.

    Timezone-aware by construction. A naive datetime reaching `Cursor.encode` is an
    `INVALID_REQUEST` (`domain/records.py`), and a clock is where that mistake would enter.
    """

    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> datetime:
        """Move time forward and return the new reading."""
        self.at = self.at + timedelta(seconds=seconds)
        return self.at
