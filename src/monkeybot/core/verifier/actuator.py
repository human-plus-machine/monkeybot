"""PRE_TOOL nudge: one ephemeral inject from a drained verdict."""

from __future__ import annotations

import logging

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.logging_utils import kv
from monkeybot.core.verifier.mailbox import VerdictMailbox

logger = logging.getLogger(__name__)


class NudgeActuator:
    """Fails open. One nudge per PRE_TOOL fire."""

    def __init__(self, mailbox: VerdictMailbox) -> None:
        self._mailbox = mailbox

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.PRE_TOOL, self.on_pre_tool)

    async def on_pre_tool(self, payload: HookPayload) -> None:
        try:
            text = self._mailbox.take_nudge(payload.thread_id)
            if not text:
                return
            existing = (payload.inject_text or "").rstrip()
            payload.inject_text = f"{existing}\n\n{text}" if existing else text
            logger.info(
                "verifier nudge injected %s",
                kv(thread_id=payload.thread_id),
            )
        except Exception:
            logger.warning(
                "nudge actuator failed %s",
                kv(thread_id=payload.thread_id),
                exc_info=True,
            )
