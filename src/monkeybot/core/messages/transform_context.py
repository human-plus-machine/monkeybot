"""Agent-facing history transforms (before provider conversion).

Keeps UI-only / internal blocks out of the agent working set and repairs tool
turn integrity for replay. Does not resolve attachments or apply pressure shaping
— those belong in :mod:`monkeybot.core.messages.convert_provider`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages.tool_integrity import repair_tool_turn_integrity
from monkeybot.core.types.content_blocks import (
    ActionRequired,
    AttachmentDescriptor,
    ContentBlock,
    FrontendToolRequest,
    SystemNotification,
    ToolConfirmationRequest,
)

logger = logging.getLogger(__name__)

# Blocks that may appear in agent/history views but must not reach providers.
_PROVIDER_EXCLUDED_BLOCKS: tuple[type[ContentBlock], ...] = (
    AttachmentDescriptor,
    ToolConfirmationRequest,
    FrontendToolRequest,
    ActionRequired,
    SystemNotification,
)


def _strip_provider_excluded(messages: Sequence[Message]) -> list[Message]:
    out: list[Message] = []
    for msg in messages:
        kept = [b for b in msg.content if not isinstance(b, _PROVIDER_EXCLUDED_BLOCKS)]
        if not kept and msg.content:
            logger.debug(
                "transform_context drop_ui_only_turn %s",
                kv(action="drop_ui_only_turn", role=msg.role, blocks=len(msg.content)),
            )
            continue
        if len(kept) == len(msg.content):
            out.append(msg)
        else:
            out.append(Message(role=msg.role, content=kept))
    return out


def transform_context(messages: Sequence[Message]) -> list[Message]:
    """Agent-facing history → cleaned message list for conversion.

    Steps:
    1. Repair broken tool-call / tool-result pairing (in-memory only).
    2. Strip UI-only content blocks that must never reach a provider.
    """
    repaired = repair_tool_turn_integrity(list(messages))
    return _strip_provider_excluded(repaired)
