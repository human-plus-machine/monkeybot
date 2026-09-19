"""Non-blocking per-thread mailbox for completed verifier verdicts."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import VerifierVerdict

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_THREAD_CAP = 256
_PER_THREAD_MAX = 16


def _cap(store: OrderedDict[str, _T]) -> None:
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
        self._ready: OrderedDict[str, deque[VerifierVerdict]] = OrderedDict()
        self._replans: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._last: OrderedDict[str, VerifierVerdict] = OrderedDict()
        self._pending: OrderedDict[str, int] = OrderedDict()
        self._active: OrderedDict[str, ActiveNudge] = OrderedDict()
        self._current_signals: OrderedDict[str, tuple[str, frozenset[str]]] = OrderedDict()

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
        text: str,
    ) -> bool:
        """Arm a sticky nudge when the verdict's signals are still live (or unpublished)."""
        note = text.strip()
        if not note:
            return False
        incoming = tuple(dict.fromkeys(s for s in verdict.triggering_signals if s))
        current = self._current_signals.get(thread_id)
        if current is not None and current[0] != request_id:
            return False
        if current is not None and incoming and not (set(incoming) & current[1]):
            return False
        existing = self._active.get(thread_id)
        if (
            existing is not None
            and existing.request_id == request_id
            and set(existing.triggering_signals) & set(incoming)
        ):
            return False
        self._active[thread_id] = ActiveNudge(
            request_id=request_id,
            verdict_id=verdict.verdict_id,
            text=note,
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
        """Drop request-local intervention state after the final drain."""
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
