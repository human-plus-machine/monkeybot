"""Non-blocking mailbox for completed verifier verdicts."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from typing import Any, TypeVar

from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import VerifierVerdict

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_THREAD_CAP = 256
_PER_SCOPE_MAX = 16
ScopeKey = tuple[str, str]


def _cap(store: OrderedDict[Any, _T]) -> None:
    while len(store) > _THREAD_CAP:
        store.popitem(last=False)


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge.

    Deposits are accepted only for the currently opened ``(thread_id, request_id)``.
    There is no implicit auto-open: callers must :meth:`open_request` first.
    """

    def __init__(self) -> None:
        self._ready: OrderedDict[ScopeKey, deque[VerifierVerdict]] = OrderedDict()
        self._nudges: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._replans: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._last: OrderedDict[str, VerifierVerdict] = OrderedDict()
        self._pending: OrderedDict[ScopeKey, int] = OrderedDict()
        self._current: OrderedDict[str, str] = OrderedDict()

    @staticmethod
    def _scope(thread_id: str, request_id: str) -> ScopeKey:
        return (thread_id, request_id)

    def _is_open(self, thread_id: str, request_id: str) -> bool:
        return self._current.get(thread_id) == request_id

    def _accept(self, thread_id: str, request_id: str) -> bool:
        """Allow deposits only for the open request. Never auto-open."""
        return bool(request_id) and self._is_open(thread_id, request_id)

    def _drop_closed(
        self, thread_id: str, request_id: str, kind: str, **extra: object
    ) -> None:
        logger.info(
            "verifier %s dropped closed request %s",
            kind,
            kv(thread_id=thread_id, request_id=request_id, **extra),
        )

    def open_request(self, thread_id: str, request_id: str) -> None:
        """Mark ``request_id`` as the only request that may deposit on this thread."""
        self._current[thread_id] = request_id
        self._current.move_to_end(thread_id)
        _cap(self._current)

    def put(self, thread_id: str, verdict: VerifierVerdict) -> bool:
        """Deposit a verdict. Requests that are not currently open are dropped."""
        if not self._accept(thread_id, verdict.request_id):
            self._drop_closed(
                thread_id,
                verdict.request_id,
                "verdict",
                verdict_id=verdict.verdict_id,
            )
            return False
        key = self._scope(thread_id, verdict.request_id)
        bucket = self._ready.get(key)
        if bucket is None:
            bucket = deque(maxlen=_PER_SCOPE_MAX)
            self._ready[key] = bucket
        else:
            self._ready.move_to_end(key)
        bucket.append(verdict)
        self.set_last(thread_id, verdict)
        _cap(self._ready)
        return True

    def last(self, thread_id: str) -> VerifierVerdict | None:
        return self._last.get(thread_id)

    def set_last(self, thread_id: str, verdict: VerifierVerdict) -> None:
        self._last[thread_id] = verdict
        self._last.move_to_end(thread_id)
        _cap(self._last)

    def mark_pending(self, thread_id: str, request_id: str = "") -> None:
        """A judge call is in flight; the turn tail may wait out its grace."""
        if not self._accept(thread_id, request_id):
            self._drop_closed(thread_id, request_id, "pending")
            return
        key = self._scope(thread_id, request_id)
        self._pending[key] = self._pending.get(key, 0) + 1
        self._pending.move_to_end(key)
        _cap(self._pending)

    def clear_pending(self, thread_id: str, request_id: str = "") -> None:
        key = self._scope(thread_id, request_id)
        remaining = self._pending.get(key, 0) - 1
        if remaining > 0:
            self._pending[key] = remaining
        else:
            self._pending.pop(key, None)

    def pending(self, thread_id: str, request_id: str | None = None) -> bool:
        if request_id is not None:
            return self._pending.get(self._scope(thread_id, request_id), 0) > 0
        return any(tid == thread_id and count > 0 for (tid, _), count in self._pending.items())

    def put_nudge(self, thread_id: str, request_id: str, text: str) -> None:
        if not self._accept(thread_id, request_id):
            self._drop_closed(thread_id, request_id, "nudge")
            return
        self._put_note(self._nudges, thread_id, request_id, text)
        _cap(self._nudges)

    def take_nudge(self, thread_id: str, request_id: str) -> str | None:
        return self._take_note(self._nudges, thread_id, request_id)

    def put_replan(self, thread_id: str, request_id: str, text: str) -> None:
        if not self._accept(thread_id, request_id):
            self._drop_closed(thread_id, request_id, "replan")
            return
        self._put_note(self._replans, thread_id, request_id, text)
        _cap(self._replans)

    def take_replan(self, thread_id: str, request_id: str) -> str | None:
        return self._take_note(self._replans, thread_id, request_id)

    def take_ready(self, thread_id: str, request_id: str | None = None) -> list[VerifierVerdict]:
        if request_id is not None:
            bucket = self._ready.pop(self._scope(thread_id, request_id), None)
            return list(bucket) if bucket else []
        keys = [key for key in self._ready if key[0] == thread_id]
        ready: list[VerifierVerdict] = []
        for key in keys:
            bucket = self._ready.pop(key, None)
            if bucket:
                ready.extend(bucket)
        return ready

    def clear_request(self, thread_id: str, request_id: str) -> None:
        """Drop request-local state after the final drain; reject later deposits."""
        key = self._scope(thread_id, request_id)
        self._ready.pop(key, None)
        self._pending.pop(key, None)
        last = self._last.get(thread_id)
        if last is not None and last.request_id == request_id:
            self._last.pop(thread_id, None)
        nudge = self._nudges.get(thread_id)
        if nudge is not None and nudge[0] == request_id:
            self._nudges.pop(thread_id, None)
        replan = self._replans.get(thread_id)
        if replan is not None and replan[0] == request_id:
            self._replans.pop(thread_id, None)
        if self._current.get(thread_id) == request_id:
            self._current.pop(thread_id, None)

    @staticmethod
    def _put_note(
        store: OrderedDict[str, tuple[str, str]], thread_id: str, request_id: str, text: str
    ) -> None:
        note = text.strip()
        if not note:
            return
        store[thread_id] = (request_id, note)
        store.move_to_end(thread_id)

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
