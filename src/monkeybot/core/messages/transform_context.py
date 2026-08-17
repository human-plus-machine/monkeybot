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
    RedactedThinking,
    SystemNotification,
    Thinking,
    ToolConfirmationRequest,
    ToolRequest,
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


def _strip_completed_turn_thinking(messages: Sequence[Message]) -> list[Message]:
    """Drop Thinking from final-text assistant turns before provider conversion.

    Chat-history persistence may store Thinking on completed replies for UI
    reload, but those blocks must not enter later provider payloads (token
    bloat / stale signatures). Assistant rows that include ``ToolRequest`` keep
    Thinking so mid-turn Anthropic/Gemini signature replay still works.
    """
    out: list[Message] = []
    for msg in messages:
        if msg.role != "assistant":
            out.append(msg)
            continue
        if any(isinstance(b, ToolRequest) for b in msg.content):
            out.append(msg)
            continue
        kept = [
            b for b in msg.content if not isinstance(b, (Thinking, RedactedThinking))
        ]
        if not kept and msg.content:
            logger.debug(
                "transform_context drop_thinking_only_turn %s",
                kv(action="drop_thinking_only_turn", role=msg.role, blocks=len(msg.content)),
            )
            continue
        if len(kept) == len(msg.content):
            out.append(msg)
        else:
            out.append(Message(role=msg.role, content=kept))
    return out


def _coalesce_adjacent_same_role(messages: Sequence[Message]) -> list[Message]:
    """Merge consecutive same-role messages after UI-only turns are dropped.

    Dropping an interior all-UI row can leave adjacent equal roles
    (``user → assistant → user(UI) → assistant`` → ``user → assistant → assistant``).
    Anthropic and Gemini reject that wire shape, so coalesce content into one
    message per run of the same role.
    """
    if not messages:
        return []
    out: list[Message] = [messages[0]]
    for msg in messages[1:]:
        prev = out[-1]
        if msg.role == prev.role:
            out[-1] = Message(role=prev.role, content=[*prev.content, *msg.content])
        else:
            out.append(msg)
    return out


def transform_context(messages: Sequence[Message]) -> list[Message]:
    """Agent-facing history → cleaned message list for conversion.

    Steps:
    1. Repair broken tool-call / tool-result pairing (in-memory only).
    2. Strip UI-only content blocks that must never reach a provider.
    3. Strip Thinking from completed (non-tool-request) assistant turns.
    4. Coalesce adjacent same-role messages left by dropped UI-only turns.
    """
    repaired = repair_tool_turn_integrity(list(messages))
    stripped = _strip_provider_excluded(repaired)
    without_prior_thinking = _strip_completed_turn_thinking(stripped)
    return _coalesce_adjacent_same_role(without_prior_thinking)
