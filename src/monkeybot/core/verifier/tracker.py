"""Deterministic in-loop progress tracker. Computes suspicion, not verdicts."""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from monkeybot.core.config.settings import VerifierTrackerConfig
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.goal_ledger import ConstraintKind
from monkeybot.core.runtime.events import VerifierVerdict
from monkeybot.core.verifier.judge import JudgeWorker
from monkeybot.core.verifier.ledger import GoalLedger
from monkeybot.core.verifier.mailbox import VerdictMailbox
from monkeybot.core.verifier.match import (
    LEDGER_SIGNALS,
    READ_TOOLS,
    WRITE_TOOLS,
    constraint_matches,
    glob_match,
    path_args,
    verdict_status,
)
from monkeybot.core.verifier.port import EvidenceBundle

logger = logging.getLogger(__name__)

_THREAD_STATE_CAP = 256
_BUDGET_FRACTION = 0.5


@dataclass
class _ThreadTrack:
    files_read: set[str] = field(default_factory=set)
    write_counts: dict[str, int] = field(default_factory=dict)
    error_streak: int = 0
    no_text_turns: int = 0
    inner_turn: int = 0
    emitted_this_turn: bool = False
    seen_tool: bool = False
    seen_provider: bool = False


class ProgressTracker:
    """Write-side observer. Cold record → no accumulation signal."""

    def __init__(
        self,
        mailbox: VerdictMailbox,
        *,
        ledger_fn: Callable[[], GoalLedger | None],
        config: VerifierTrackerConfig,
        judge: JudgeWorker | None = None,
    ) -> None:
        self._mailbox = mailbox
        self._ledger_fn = ledger_fn
        self._config = config
        self._judge = judge
        self._by_thread: OrderedDict[str, _ThreadTrack] = OrderedDict()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.POST_TOOL, self.on_post_tool)
        manager.register(HookEvent.AFTER_PROVIDER_RESPONSE, self.on_after_provider)
        manager.register(HookEvent.POST_TURN, self.on_post_turn)

    async def on_post_tool(self, payload: HookPayload) -> None:
        try:
            self._observe_tool(payload)
        except Exception:
            logger.warning(
                "progress_tracker post_tool failed %s",
                kv(thread_id=payload.thread_id),
                exc_info=True,
            )

    async def on_after_provider(self, payload: HookPayload) -> None:
        try:
            self._observe_provider(payload)
        except Exception:
            logger.warning(
                "progress_tracker after_provider failed %s",
                kv(thread_id=payload.thread_id),
                exc_info=True,
            )

    async def on_post_turn(self, payload: HookPayload) -> None:
        try:
            self._observe_turn_end(payload)
        except Exception:
            logger.warning(
                "progress_tracker post_turn failed %s",
                kv(thread_id=payload.thread_id),
                exc_info=True,
            )

    def _state(self, thread_id: str) -> _ThreadTrack:
        existing = self._by_thread.get(thread_id)
        if existing is not None:
            self._by_thread.move_to_end(thread_id)
            return existing
        state = _ThreadTrack()
        self._by_thread[thread_id] = state
        while len(self._by_thread) > _THREAD_STATE_CAP:
            self._by_thread.popitem(last=False)
        return state

    def _observe_tool(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        warm = state.seen_tool
        name = payload.tool_name or ""
        args = payload.tool_args
        paths = path_args(args)
        if name in READ_TOOLS:
            state.files_read.update(paths)
        if name in WRITE_TOOLS:
            for path in paths:
                state.write_counts[path] = state.write_counts.get(path, 0) + 1
        if payload.tool_error:
            state.error_streak += 1
        else:
            state.error_streak = 0
        signals: list[str] = []
        if warm and state.error_streak >= self._config.suspicion_threshold:
            signals.append("error_streak")
        if warm and name in WRITE_TOOLS:
            unread = [p for p in paths if p not in state.files_read]
            if unread:
                signals.append("write_without_read")
            if any(state.write_counts.get(p, 0) >= self._config.suspicion_threshold for p in paths):
                signals.append("rewrite_churn")
        signals.extend(self._ledger_signals(payload.thread_id, name, args))
        state.seen_tool = True
        self._maybe_emit(payload, state, signals)

    def _observe_provider(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        warm = state.seen_provider
        state.inner_turn = payload.inner_turn or state.inner_turn
        state.emitted_this_turn = False
        call_tokens = 0
        if payload.usage:
            call_tokens = int(payload.usage.get("input_tokens") or 0) + int(
                payload.usage.get("output_tokens") or 0
            )
            if self._judge is not None and call_tokens:
                self._judge.note_agent_tokens(payload.request_id, call_tokens)
        text = (payload.assistant_text or "").strip()
        has_tools = bool(payload.tool_requests)
        if has_tools and not text:
            state.no_text_turns += 1
        else:
            state.no_text_turns = 0
        # budget_burn / no_progress fire on healthy long tool loops. Log only
        # until a later phase proves precision; do not put them on the mailbox.
        if warm and state.no_text_turns >= self._config.suspicion_threshold:
            logger.info(
                "progress_tracker signal %s",
                kv(
                    thread_id=payload.thread_id,
                    signals="no_progress",
                    mailbox=False,
                ),
            )
        window = payload.ctx.context_window_tokens
        if warm and window > 0 and call_tokens >= int(window * _BUDGET_FRACTION):
            logger.info(
                "progress_tracker signal %s",
                kv(
                    thread_id=payload.thread_id,
                    signals="budget_burn",
                    mailbox=False,
                ),
            )
        state.seen_provider = True

    def _observe_turn_end(self, payload: HookPayload) -> None:
        signals = self._ledger_signals(payload.thread_id, "", None, turn_end=True)
        state = self._state(payload.thread_id)
        self._maybe_emit(payload, state, signals)

    def _ledger_signals(
        self,
        thread_id: str,
        tool_name: str,
        args: dict[str, Any] | None,
        *,
        turn_end: bool = False,
    ) -> list[str]:
        ledger = self._ledger_fn()
        if ledger is None:
            return []
        view = ledger.resolved_intent(thread_id)
        if view is None:
            return []
        signals: list[str] = []
        if tool_name:
            for constraint in view.standing_constraints:
                if constraint.kind == ConstraintKind.FREE_TEXT:
                    continue
                if not constraint_matches(constraint, tool_name=tool_name, args=args):
                    continue
                count = 0
                for keyed, n in view.correction_history.items():
                    if keyed.match_key == constraint.match_key:
                        count = n
                        break
                if count >= 2:
                    signals.append("repeat_correction")
                else:
                    signals.append("constraint_touch")
        if turn_end and view.active_goal and view.active_goal.done_when:
            written = set()
            existing = self._by_thread.get(thread_id)
            if existing is not None:
                written = set(existing.write_counts)
            unmet = [
                item
                for item in view.active_goal.done_when
                if _looks_like_path(item) and not _done_when_satisfied(item, written)
            ]
            if unmet:
                signals.append("done_unmet")
        return list(dict.fromkeys(signals))

    def _maybe_emit(
        self,
        payload: HookPayload,
        state: _ThreadTrack,
        signals: list[str],
    ) -> None:
        if not signals or state.emitted_this_turn:
            return
        inner = payload.inner_turn or state.inner_turn or 1
        ledger_hit = [s for s in signals if s in LEDGER_SIGNALS]
        other = [s for s in signals if s not in LEDGER_SIGNALS]
        signals = ledger_hit if inner < self._config.min_turn_before_verdict else ledger_hit + other
        if not signals:
            return
        if self._judge is not None:
            self._judge.enqueue(
                EvidenceBundle(
                    thread_id=payload.thread_id,
                    request_id=payload.request_id,
                    inner_turn=inner,
                    signals=tuple(signals),
                )
            )
            state.emitted_this_turn = True
            return
        status, confidence = verdict_status(signals)
        verdict = VerifierVerdict(
            request_id=payload.request_id,
            verdict_id=str(uuid.uuid4()),
            checkpoint_id=f"{payload.request_id}:{inner}",
            status=status,
            severity="none",
            confidence=confidence,
            rationale=", ".join(signals),
            triggering_signals=tuple(signals),
        )
        self._mailbox.put(payload.thread_id, verdict)
        state.emitted_this_turn = True
        logger.info(
            "progress_tracker verdict %s",
            kv(
                thread_id=payload.thread_id,
                status=status,
                signals=",".join(signals),
            ),
        )


def _looks_like_path(item: str) -> bool:
    return "/" in item or item.endswith(".txt") or item.endswith(".md")


def _done_when_satisfied(item: str, written: set[str]) -> bool:
    needle = item.replace("\\", "/").lstrip("./")
    if not needle:
        return False
    for path in written:
        normalized = path.replace("\\", "/").lstrip("./")
        if normalized == needle or normalized.endswith("/" + needle):
            return True
        if glob_match(normalized, needle):
            return True
    return False
