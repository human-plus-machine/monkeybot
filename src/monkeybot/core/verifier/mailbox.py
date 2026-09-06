"""Non-blocking per-thread mailbox for completed verifier verdicts."""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import TypeVar

from monkeybot.core.runtime.events import VerifierVerdict

_T = TypeVar("_T")

_THREAD_CAP = 256
_PER_THREAD_MAX = 16


def _cap(store: OrderedDict[str, _T]) -> None:
    while len(store) > _THREAD_CAP:
        store.popitem(last=False)


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge."""

    def __init__(self) -> None:
        self._ready: OrderedDict[str, deque[VerifierVerdict]] = OrderedDict()
        self._nudges: OrderedDict[str, str] = OrderedDict()
        self._replans: OrderedDict[str, str] = OrderedDict()
        self._last: OrderedDict[str, VerifierVerdict] = OrderedDict()

    def put(self, thread_id: str, verdict: VerifierVerdict) -> None:
        bucket = self._ready.get(thread_id)
        if bucket is None:
            bucket = deque(maxlen=_PER_THREAD_MAX)
            self._ready[thread_id] = bucket
        else:
            self._ready.move_to_end(thread_id)
        bucket.append(verdict)
        self.set_last(thread_id, verdict)
        while len(self._ready) > _THREAD_CAP:
            self._ready.popitem(last=False)

    def last(self, thread_id: str) -> VerifierVerdict | None:
        return self._last.get(thread_id)

    def set_last(self, thread_id: str, verdict: VerifierVerdict) -> None:
        self._last[thread_id] = verdict
        self._last.move_to_end(thread_id)
        _cap(self._last)

    def put_nudge(self, thread_id: str, text: str) -> None:
        self._put_note(self._nudges, thread_id, text)

    def take_nudge(self, thread_id: str) -> str | None:
        return self._nudges.pop(thread_id, None)

    def put_replan(self, thread_id: str, text: str) -> None:
        self._put_note(self._replans, thread_id, text)

    def take_replan(self, thread_id: str) -> str | None:
        return self._replans.pop(thread_id, None)

    def take_ready(self, thread_id: str) -> list[VerifierVerdict]:
        bucket = self._ready.pop(thread_id, None)
        if not bucket:
            return []
        return list(bucket)

    @staticmethod
    def _put_note(store: OrderedDict[str, str], thread_id: str, text: str) -> None:
        note = text.strip()
        if not note:
            return
        store[thread_id] = note
        store.move_to_end(thread_id)
        _cap(store)
