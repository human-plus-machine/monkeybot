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
        self._nudges: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._replans: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._last: OrderedDict[str, VerifierVerdict] = OrderedDict()
        self._pending: OrderedDict[str, int] = OrderedDict()

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

    def mark_pending(self, thread_id: str) -> None:
        """A judge call is in flight; the turn tail may wait out its grace."""
        self._pending[thread_id] = self._pending.get(thread_id, 0) + 1
        self._pending.move_to_end(thread_id)
        _cap(self._pending)

    def clear_pending(self, thread_id: str) -> None:
        remaining = self._pending.get(thread_id, 0) - 1
        if remaining > 0:
            self._pending[thread_id] = remaining
        else:
            self._pending.pop(thread_id, None)

    def pending(self, thread_id: str) -> bool:
        return self._pending.get(thread_id, 0) > 0

    def put_nudge(self, thread_id: str, request_id: str, text: str) -> None:
        self._put_note(self._nudges, thread_id, request_id, text)

    def take_nudge(self, thread_id: str, request_id: str) -> str | None:
        return self._take_note(self._nudges, thread_id, request_id)

    def put_replan(self, thread_id: str, request_id: str, text: str) -> None:
        self._put_note(self._replans, thread_id, request_id, text)

    def take_replan(self, thread_id: str, request_id: str) -> str | None:
        return self._take_note(self._replans, thread_id, request_id)

    def take_ready(self, thread_id: str) -> list[VerifierVerdict]:
        bucket = self._ready.pop(thread_id, None)
        if not bucket:
            return []
        return list(bucket)

    @staticmethod
    def _put_note(
        store: OrderedDict[str, tuple[str, str]], thread_id: str, request_id: str, text: str
    ) -> None:
        note = text.strip()
        if not note:
            return
        store[thread_id] = (request_id, note)
        store.move_to_end(thread_id)
        _cap(store)

    @staticmethod
    def _take_note(
        store: OrderedDict[str, tuple[str, str]], thread_id: str, request_id: str
    ) -> str | None:
        """Pop the note. Request-scoped: a note from another request is discarded."""
        entry = store.pop(thread_id, None)
        if entry is None:
            return None
        note_request_id, note = entry
        if note_request_id != request_id:
            return None
        return note
