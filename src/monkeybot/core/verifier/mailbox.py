"""Non-blocking mailbox for completed verifier verdicts."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.verifier.intervention import correction_text, ordered_signals

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_THREAD_CAP = 256
_PER_SCOPE_MAX = 16
_PER_THREAD_MAX = _PER_SCOPE_MAX
ScopeKey = tuple[str, str]


def _cap(store: OrderedDict[Any, _T]) -> None:
    while len(store) > _THREAD_CAP:
        store.popitem(last=False)


@dataclass
class ActiveNudge:
    """Request-scoped sticky correction shown until tracker signals recover."""

    request_id: str
    verdict_id: str
    text: str
    triggering_signals: tuple[str, ...]
    injections: int = 0


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge."""

    def __init__(self) -> None:
        self._ready: OrderedDict[ScopeKey, deque[VerifierVerdict]] = OrderedDict()
        self._replans: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._last: OrderedDict[str, VerifierVerdict] = OrderedDict()
        self._pending: OrderedDict[ScopeKey, int] = OrderedDict()
        self._active: OrderedDict[str, ActiveNudge] = OrderedDict()
        self._current_signals: OrderedDict[str, tuple[str, frozenset[str]]] = OrderedDict()
        self._closed: OrderedDict[ScopeKey, None] = OrderedDict()

    @staticmethod
    def _scope(thread_id: str, request_id: str) -> ScopeKey:
        return (thread_id, request_id)

    def put(self, thread_id: str, verdict: VerifierVerdict) -> bool:
        """Deposit a verdict. Closed requests are dropped, never leaked to the next turn."""
        key = self._scope(thread_id, verdict.request_id)
        if key in self._closed:
            logger.info(
                "verifier verdict dropped closed request %s",
                kv(
                    thread_id=thread_id,
                    request_id=verdict.request_id,
                    verdict_id=verdict.verdict_id,
                ),
            )
            return False
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

    def set_current_signals(self, thread_id: str, request_id: str, signals: Iterable[str]) -> None:
        """Publish the tracker's live suspicion set; drop a recovered active nudge."""
        sigs = frozenset(s for s in signals if s)
        self._current_signals[thread_id] = (request_id, sigs)
        self._current_signals.move_to_end(thread_id)
        _cap(self._current_signals)
        active = self._active.get(thread_id)
        if active is None:
            return
        if active.request_id != request_id or not (set(active.triggering_signals) & sigs):
            logger.info(
                "verifier nudge recovered %s",
                kv(
                    thread_id=thread_id,
                    request_id=request_id,
                    verdict_id=active.verdict_id,
                    injections=active.injections,
                    signals=",".join(sorted(sigs)),
                ),
            )
            self._active.pop(thread_id, None)

    def activate_nudge(
        self,
        thread_id: str,
        request_id: str,
        verdict: VerifierVerdict,
        text: str | None = None,
    ) -> bool:
        """Arm a sticky nudge when the verdict's signals are still live (or unpublished).

        Overlapping verdicts union their triggering signals and regenerate the
        trusted template so a still-live secondary signal cannot clear the nudge.
        ``text`` is ignored; actuation always uses :func:`correction_text`.
        """
        del text
        incoming = ordered_signals(verdict.triggering_signals)
        current = self._current_signals.get(thread_id)
        if current is not None and current[0] != request_id:
            return False
        if current is not None and incoming and not (set(incoming) & current[1]):
            return False
        existing = self._active.get(thread_id)
        if existing is not None and existing.request_id == request_id:
            merged = ordered_signals((*existing.triggering_signals, *incoming))
            if merged == existing.triggering_signals:
                return False
            existing.triggering_signals = merged
            existing.text = correction_text(merged)
            existing.verdict_id = verdict.verdict_id
            self._active.move_to_end(thread_id)
            return True
        self._active[thread_id] = ActiveNudge(
            request_id=request_id,
            verdict_id=verdict.verdict_id,
            text=correction_text(incoming),
            triggering_signals=incoming,
        )
        self._active.move_to_end(thread_id)
        _cap(self._active)
        return True

    def peek_nudge(self, thread_id: str, request_id: str) -> str | None:
        """Return the active correction without consuming it. Wrong request → None."""
        active = self._active.get(thread_id)
        if active is None or active.request_id != request_id:
            return None
        active.injections += 1
        return active.text

    def clear_request(self, thread_id: str, request_id: str) -> None:
        """Drop request-local intervention state after the final drain; reject later deposits."""
        key = self._scope(thread_id, request_id)
        self._ready.pop(key, None)
        self._pending.pop(key, None)
        active = self._active.get(thread_id)
        if active is not None and active.request_id == request_id:
            logger.info(
                "verifier nudge request-end %s",
                kv(
                    thread_id=thread_id,
                    request_id=request_id,
                    verdict_id=active.verdict_id,
                    injections=active.injections,
                ),
            )
            self._active.pop(thread_id, None)
        current = self._current_signals.get(thread_id)
        if current is not None and current[0] == request_id:
            self._current_signals.pop(thread_id, None)
        replan = self._replans.get(thread_id)
        if replan is not None and replan[0] == request_id:
            self._replans.pop(thread_id, None)
        self._closed[key] = None
        self._closed.move_to_end(key)
        _cap(self._closed)

    def put_replan(self, thread_id: str, request_id: str, text: str) -> None:
        self._put_note(self._replans, thread_id, request_id, text)

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
