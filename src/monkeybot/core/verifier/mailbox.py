"""Non-blocking per-thread mailbox for completed verifier verdicts."""

from __future__ import annotations

from collections import OrderedDict, deque

from monkeybot.core.runtime.events import VerifierVerdict

_THREAD_CAP = 256
_PER_THREAD_MAX = 16


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge."""

    def __init__(self) -> None:
        self._ready: OrderedDict[str, deque[VerifierVerdict]] = OrderedDict()

    def put(self, thread_id: str, verdict: VerifierVerdict) -> None:
        bucket = self._ready.get(thread_id)
        if bucket is None:
            bucket = deque(maxlen=_PER_THREAD_MAX)
            self._ready[thread_id] = bucket
        else:
            self._ready.move_to_end(thread_id)
        bucket.append(verdict)
        while len(self._ready) > _THREAD_CAP:
            self._ready.popitem(last=False)

    def take_ready(self, thread_id: str) -> list[VerifierVerdict]:
        bucket = self._ready.pop(thread_id, None)
        if not bucket:
            return []
        return list(bucket)
