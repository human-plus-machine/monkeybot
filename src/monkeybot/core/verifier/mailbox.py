"""Non-blocking mailbox for completed verifier verdicts."""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from monkeybot.core.config.settings import VERIFIER_SEVERITY_RANK
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


def _checkpoint_turn(verdict: VerifierVerdict) -> int:
    raw = verdict.checkpoint_id.rsplit(":", 1)[-1]
    try:
        return int(raw)
    except ValueError:
        return 0


def _severity_rank(verdict: VerifierVerdict) -> int:
    return VERIFIER_SEVERITY_RANK.get(verdict.severity, 0)


def _newer_verdict(current: VerifierVerdict, incoming: VerifierVerdict) -> bool:
    """True when ``incoming`` should replace ``current`` for the same request."""
    incoming_turn = _checkpoint_turn(incoming)
    current_turn = _checkpoint_turn(current)
    if incoming_turn != current_turn:
        return incoming_turn > current_turn
    return _severity_rank(incoming) >= _severity_rank(current)


@dataclass
class ActiveNudge:
    """Request-scoped sticky correction shown until tracker signals recover."""

    request_id: str
    verdict_id: str
    text: str
    triggering_signals: tuple[str, ...]
    injections: int = 0


@dataclass
class _SignalEpisode:
    """Tracker's live suspicion set plus the episode counter for each signal.

    ``epochs[signal]`` increments every time a signal re-enters ``signals``, so a
    verdict judged during an earlier episode can be told apart from one judged
    against the episode that is live now.
    """

    request_id: str
    signals: frozenset[str]
    epochs: dict[str, int]


def _fence_signals(
    episode: _SignalEpisode,
    verdict: VerifierVerdict,
    incoming: tuple[str, ...],
) -> tuple[str, ...]:
    """Keep only signals still live in the episode the verdict was judged against.

    A verdict carrying no epoch snapshot predates episode fencing, so it keeps
    every triggering signal as long as one of them is still live.
    """
    verdict_epochs = dict(verdict.triggering_signal_epochs)
    if not verdict_epochs:
        return incoming if set(incoming) & episode.signals else ()
    return ordered_signals(
        signal
        for signal in incoming
        if signal in episode.signals and episode.epochs.get(signal) == verdict_epochs.get(signal)
    )


class VerdictMailbox:
    """Loop-owned drain target. ``take_ready`` never waits on the judge."""

    def __init__(self) -> None:
        self._ready: OrderedDict[ScopeKey, deque[VerifierVerdict]] = OrderedDict()
        self._replans: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._last: OrderedDict[str, VerifierVerdict] = OrderedDict()
        self._pending: OrderedDict[ScopeKey, int] = OrderedDict()
        self._active: OrderedDict[str, ActiveNudge] = OrderedDict()
        self._episodes: OrderedDict[str, _SignalEpisode] = OrderedDict()
        self._current: OrderedDict[str, str] = OrderedDict()
        self._last_closed: OrderedDict[str, str] = OrderedDict()

    @staticmethod
    def _scope(thread_id: str, request_id: str) -> ScopeKey:
        return (thread_id, request_id)

    def _is_open(self, thread_id: str, request_id: str) -> bool:
        return self._current.get(thread_id) == request_id

    def _accept(self, thread_id: str, request_id: str) -> bool:
        """Allow deposits for the open request; auto-open when the thread is idle.

        A request that just ended cannot reopen itself. A different request may.
        """
        if self._is_open(thread_id, request_id):
            return True
        if self._current.get(thread_id) is not None:
            return False
        if self._last_closed.get(thread_id) == request_id:
            return False
        self.open_request(thread_id, request_id)
        return True

    def open_request(self, thread_id: str, request_id: str) -> None:
        """Mark ``request_id`` as the only request that may deposit on this thread."""
        self._current[thread_id] = request_id
        self._current.move_to_end(thread_id)

    def _is_stale(self, thread_id: str, verdict: VerifierVerdict) -> bool:
        """True when a newer verdict for the same request already landed."""
        current = self._last.get(thread_id)
        return (
            current is not None
            and current.request_id == verdict.request_id
            and not _newer_verdict(current, verdict)
        )

    def put(self, thread_id: str, verdict: VerifierVerdict) -> bool:
        """Deposit a verdict. Requests that are not currently open are dropped."""
        if not self._accept(thread_id, verdict.request_id):
            logger.info(
                "verifier verdict dropped closed request %s",
                kv(
                    thread_id=thread_id,
                    request_id=verdict.request_id,
                    verdict_id=verdict.verdict_id,
                ),
            )
            return False
        if self._is_stale(thread_id, verdict):
            logger.info(
                "verifier verdict dropped stale checkpoint %s",
                kv(
                    thread_id=thread_id,
                    request_id=verdict.request_id,
                    verdict_id=verdict.verdict_id,
                    checkpoint_id=verdict.checkpoint_id,
                    latest_checkpoint_id=self._last[thread_id].checkpoint_id,
                ),
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
        if self._is_stale(thread_id, verdict):
            return
        self._last[thread_id] = verdict
        self._last.move_to_end(thread_id)
        _cap(self._last)

    def mark_pending(self, thread_id: str, request_id: str = "") -> None:
        """A judge call is in flight; the turn tail may wait out its grace."""
        if not self._accept(thread_id, request_id):
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

    def _episode(self, thread_id: str, request_id: str) -> _SignalEpisode | None:
        """The live signal episode for this request, or None when it belongs elsewhere."""
        episode = self._episodes.get(thread_id)
        if episode is None or episode.request_id != request_id:
            return None
        return episode

    def set_current_signals(self, thread_id: str, request_id: str, signals: Iterable[str]) -> None:
        """Publish the tracker's live suspicion set; drop a recovered active nudge."""
        if not self._accept(thread_id, request_id):
            return
        sigs = frozenset(s for s in signals if s)
        episode = self._episode(thread_id, request_id)
        if episode is None:
            episode = _SignalEpisode(request_id=request_id, signals=frozenset(), epochs={})
            self._episodes[thread_id] = episode
        for signal in sigs - episode.signals:
            episode.epochs[signal] = episode.epochs.get(signal, 0) + 1
        episode.signals = sigs
        self._episodes.move_to_end(thread_id)
        _cap(self._episodes)
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

    def signal_epochs(
        self, thread_id: str, request_id: str, signals: Iterable[str]
    ) -> tuple[tuple[str, int], ...]:
        """Snapshot the current episode number for each live signal."""
        episode = self._episode(thread_id, request_id)
        if episode is None:
            return ()
        return tuple(
            (signal, episode.epochs[signal])
            for signal in ordered_signals(signals)
            if signal in episode.signals and signal in episode.epochs
        )

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
        if not self._accept(thread_id, request_id):
            return False
        incoming = ordered_signals(verdict.triggering_signals)
        episode = self._episodes.get(thread_id)
        if episode is not None:
            if episode.request_id != request_id:
                return False
            if incoming:
                incoming = _fence_signals(episode, verdict, incoming)
                if not incoming:
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
        self._cap_idle_threads(self._active)
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
        if self._episode(thread_id, request_id) is not None:
            self._episodes.pop(thread_id, None)
        replan = self._replans.get(thread_id)
        if replan is not None and replan[0] == request_id:
            self._replans.pop(thread_id, None)
        if self._current.get(thread_id) == request_id:
            self._current.pop(thread_id, None)
        self._last_closed[thread_id] = request_id
        self._last_closed.move_to_end(thread_id)
        _cap(self._last_closed)

    def put_replan(self, thread_id: str, request_id: str, text: str) -> None:
        if not self._accept(thread_id, request_id):
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

    def _cap_idle_threads(self, store: OrderedDict[str, Any]) -> None:
        while len(store) > _THREAD_CAP:
            victim = next((key for key in store if key not in self._current), None)
            if victim is None:
                break
            store.pop(victim, None)

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
