"""Non-blocking per-thread mailbox for completed verifier verdicts."""

from __future__ import annotations

from collections import defaultdict, deque

from monkeybot.core.runtime.events import VerifierVerdict


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge."""

    def __init__(self) -> None:
        self._ready: dict[str, deque[VerifierVerdict]] = defaultdict(deque)

    def put(self, thread_id: str, verdict: VerifierVerdict) -> None:
        self._ready[thread_id].append(verdict)

    def take_ready(self, thread_id: str) -> list[VerifierVerdict]:
        bucket = self._ready.get(thread_id)
        if not bucket:
            return []
        out = list(bucket)
        bucket.clear()
        return out
