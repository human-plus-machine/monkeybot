"""Shared cap on harness wakes that start a turn without a user message.

Background subagents and background commands each finish on their own. A burst
of completions must not keep the session in a loop of harness turns. One budget
covers both sources, and a human message resets it.
"""

from __future__ import annotations


class WakeBudget:
    """How many harness wakes may run in a row before a person speaks."""

    def __init__(self, *, max_consecutive: int = 3) -> None:
        self._max = max(1, max_consecutive)
        self._count = 0

    def note_human_turn(self) -> None:
        self._count = 0

    def allow(self) -> bool:
        return self._count < self._max

    def record(self) -> None:
        self._count += 1
