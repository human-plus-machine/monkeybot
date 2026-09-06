"""Non-blocking per-thread mailbox for completed verifier verdicts."""

from __future__ import annotations

from collections import defaultdict, deque

from monkeybot.core.runtime.events import VerifierVerdict


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge."""

    def __init__(self) -> None:
        self._ready: dict[str, deque[VerifierVerdict]] = defaultdict(deque)
        self._nudges: dict[str, deque[str]] = defaultdict(deque)
        self._replans: dict[str, deque[str]] = defaultdict(deque)
        self._last: dict[str, VerifierVerdict] = {}

    def put(self, thread_id: str, verdict: VerifierVerdict) -> None:
        self._ready[thread_id].append(verdict)
        self._last[thread_id] = verdict

    def last(self, thread_id: str) -> VerifierVerdict | None:
        return self._last.get(thread_id)

    def put_nudge(self, thread_id: str, text: str) -> None:
        if text.strip():
            self._nudges[thread_id].append(text.strip())

    def take_nudge(self, thread_id: str) -> str | None:
        bucket = self._nudges.get(thread_id)
        if not bucket:
            return None
        return bucket.popleft()

    def put_replan(self, thread_id: str, text: str) -> None:
        if text.strip():
            self._replans[thread_id].append(text.strip())

    def take_replan(self, thread_id: str) -> str | None:
        bucket = self._replans.get(thread_id)
        if not bucket:
            return None
        return bucket.popleft()

    def take_ready(self, thread_id: str) -> list[VerifierVerdict]:
        bucket = self._ready.get(thread_id)
        if not bucket:
            return []
        out = list(bucket)
        bucket.clear()
        return out
