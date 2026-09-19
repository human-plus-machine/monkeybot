"""BEFORE_PROVIDER sticky nudge: labeled verifier block from an active verdict."""

from __future__ import annotations

import logging

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.prompts.headings import VERIFIER_HEADING
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.verifier.mailbox import VerdictMailbox

logger = logging.getLogger(__name__)

_STICKY_TAIL = (
    "This instruction remains in effect until you change approach or the "
    "current request ends. Prefer it over continuing the same failing action."
)


def format_nudge_block(text: str) -> str:
    """Labeled system-level correction. Always starts with ``[Verifier]``."""
    body = text.strip()
    if not body:
        return ""
    if not body.startswith("[Verifier]"):
        body = f"[Verifier] {body}"
    return f"{body}\n{_STICKY_TAIL}"


def _append_verifier_block(system: Message, block: str) -> Message:
    extra = block.strip()
    if not extra:
        return system
    base = "".join(b.text for b in system.content if isinstance(b, Text))
    if VERIFIER_HEADING.strip() in base and extra.split("\n", 1)[0] in base:
        return system
    return Message(role="system", content=[Text(text=f"{base}{VERIFIER_HEADING}{extra}\n")])


class NudgeActuator:
    """Fails open. Peeks the active nudge into every provider call until recovery."""

    def __init__(self, mailbox: VerdictMailbox) -> None:
        self._mailbox = mailbox

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.BEFORE_PROVIDER_REQUEST, self.on_before_provider)

    async def on_before_provider(self, payload: HookPayload) -> None:
        try:
            text = self._mailbox.peek_nudge(payload.thread_id, payload.request_id)
            if not text or payload.provider_messages is None:
                return
            block = format_nudge_block(text)
            if not block:
                return
            messages = list(payload.provider_messages)
            if not messages:
                payload.provider_messages = [
                    Message(
                        role="system", content=[Text(text=f"{VERIFIER_HEADING.strip()}\n{block}\n")]
                    )
                ]
            else:
                first = messages[0]
                if first.role == "system":
                    messages[0] = _append_verifier_block(first, block)
                else:
                    messages.insert(
                        0,
                        Message(
                            role="system",
                            content=[Text(text=f"{VERIFIER_HEADING.strip()}\n{block}\n")],
                        ),
                    )
                payload.provider_messages = messages
            logger.info(
                "verifier nudge injected %s",
                kv(thread_id=payload.thread_id, request_id=payload.request_id),
            )
        except Exception:
            logger.warning(
                "nudge actuator failed %s",
                kv(thread_id=payload.thread_id),
                exc_info=True,
            )
