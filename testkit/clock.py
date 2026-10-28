"""Injectable epoch-millisecond clock.

Long-running loops take ``clock=fake.now_ms``. One-shot ``now_ms`` parameters
take ``now_ms=fake.now_ms()``. ``advance`` is what the ``advance_clock`` step calls.
"""

from __future__ import annotations


class FakeClock:
    """Frozen epoch-ms clock. Time moves only when ``advance`` is called."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        """Current time in epoch milliseconds."""
        return self._now_ms

    def advance(self, ms: int) -> int:
        """Move forward by ``ms`` and return the new time."""
        if ms < 0:
            raise ValueError("ms must be >= 0")
        self._now_ms += ms
        return self._now_ms
