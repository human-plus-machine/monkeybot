"""Memory hook: enqueue is owned by history; this hook does L2 recall and writer drain."""

from __future__ import annotations

import logging
from typing import Any

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.memory.ids import conversation_wing
from monkeybot.core.memory.ingest import workspace_id_from_env
from monkeybot.core.memory.observability import Timer, log_event, memory_span
from monkeybot.core.memory.palace import CONVERSATION_ROOM, format_recall_lines

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_S = 5.0


class MemoryHook:
    """Wire MemPalace read/drain to :class:`HookManager` events.

    Automatic ingest happens when chat history is committed (see ``ingest.py``).
    This hook injects L2 workspace recall and wakes the per-agent writer.
    """

    def __init__(self, subsystem: Any) -> None:
        self._memory = subsystem

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.PRE_TURN, self.on_pre_turn)
        manager.register(HookEvent.POST_TURN, self.on_post_turn)
        manager.register(HookEvent.SESSION_END, self.on_session_end)

    async def on_pre_turn(self, payload: HookPayload) -> None:
        timer = Timer()
        wing = conversation_wing(workspace_id_from_env())
        thread_id = payload.thread_id
        with memory_span(
            "monkeybot.memory.recall",
            **{
                "memory.operation": "recall",
                "memory.wing": wing,
                "memory.backend": self._memory.backend,
            },
        ):
            try:
                drawers = await self._memory.recall(
                    wing=wing, room=CONVERSATION_ROOM, thread_id=thread_id
                )
            except Exception as exc:
                logger.warning("memory L2 recall failed: %r", exc)
                return
        lines = format_recall_lines(drawers)
        log_event(
            "recall",
            memory_status="ok",
            memory_backend=self._memory.backend,
            memory_result_count=len(lines),
            memory_duration_ms=round(timer.ms(), 1),
        )
        if lines:
            heading = (
                "Workspace conversation recall (verbatim; prefer `mempalace search` for older turns):"
            )
            existing = (payload.inject_text or "").rstrip()
            payload.inject_text = f"{existing}\n\n{heading}" if existing else heading
            payload.inject_memory_lines = list(payload.inject_memory_lines) + lines

    async def on_post_turn(self, payload: HookPayload) -> None:
        del payload
        self._memory.wake_writer()

    async def on_session_end(self, payload: HookPayload) -> None:
        del payload
        try:
            await self._memory.drain_writer(timeout_s=_DRAIN_TIMEOUT_S)
        except Exception as exc:
            logger.warning("memory session-end drain failed: %r", exc)
