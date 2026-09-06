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
        self._nudges: dict[str, deque[str]] = {}
        self._replans: dict[str, deque[str]] = {}
        self._last: dict[str, VerifierVerdict] = {}

    def put(self, thread_id: str, verdict: VerifierVerdict) -> None:
        bucket = self._ready.get(thread_id)
        if bucket is None:
            bucket = deque(maxlen=_PER_THREAD_MAX)
            self._ready[thread_id] = bucket
        else:
            self._ready.move_to_end(thread_id)
        bucket.append(verdict)
        self._last[thread_id] = verdict
        while len(self._ready) > _THREAD_CAP:
            tid, _ = self._ready.popitem(last=False)
            self._nudges.pop(tid, None)
            self._replans.pop(tid, None)
            self._last.pop(tid, None)

    def last(self, thread_id: str) -> VerifierVerdict | None:
        return self._last.get(thread_id)

    def set_last(self, thread_id: str, verdict: VerifierVerdict) -> None:
        self._last[thread_id] = verdict

    def put_nudge(self, thread_id: str, text: str) -> None:
        self._append_note(self._nudges, thread_id, text)

    def take_nudge(self, thread_id: str) -> str | None:
        return self._take_note(self._nudges, thread_id)

    def put_replan(self, thread_id: str, text: str) -> None:
        self._append_note(self._replans, thread_id, text)

    def take_replan(self, thread_id: str) -> str | None:
        return self._take_note(self._replans, thread_id)

    def take_ready(self, thread_id: str) -> list[VerifierVerdict]:
        bucket = self._ready.pop(thread_id, None)
        if not bucket:
            return []
        return list(bucket)

    @staticmethod
    def _append_note(store: dict[str, deque[str]], thread_id: str, text: str) -> None:
        note = text.strip()
        if not note:
            return
        bucket = store.get(thread_id)
        if bucket is None:
            bucket = deque(maxlen=_PER_THREAD_MAX)
            store[thread_id] = bucket
        bucket.append(note)

    @staticmethod
    def _take_note(store: dict[str, deque[str]], thread_id: str) -> str | None:
        bucket = store.get(thread_id)
        if not bucket:
            return None
        note = bucket.popleft()
        if not bucket:
            store.pop(thread_id, None)
        return note
