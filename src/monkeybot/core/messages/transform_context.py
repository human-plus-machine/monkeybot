"""Agent-facing history transforms (before provider conversion).

Keeps UI-only / internal blocks out of the agent working set and repairs tool
turn integrity for replay. Does not resolve attachments or apply pressure shaping
— those belong in :mod:`monkeybot.core.messages.convert_provider`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

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


def provider_visible_blocks(msg: Message) -> list[ContentBlock]:
    """Blocks of ``msg`` that survive :func:`transform_context` into the provider payload.

    Mirrors the strip rules: UI-only blocks never reach providers, and Thinking is
    kept only on assistant rows that carry a ``ToolRequest`` (signature replay).
    """
    has_tool_request = any(isinstance(b, ToolRequest) for b in msg.content)
    drop_thinking = msg.role == "assistant" and not has_tool_request
    return [
        b
        for b in msg.content
        if not isinstance(b, _PROVIDER_EXCLUDED_BLOCKS)
        and not (drop_thinking and isinstance(b, (Thinking, RedactedThinking)))
    ]


def is_provider_visible(msg: Message) -> bool:
    """True when ``msg`` survives :func:`transform_context` (rows with no content pass through)."""
    return bool(provider_visible_blocks(msg)) or not msg.content


def _strip_non_provider_blocks(messages: Sequence[Message]) -> list[Message]:
    """Drop UI-only blocks and completed-reply Thinking via :func:`provider_visible_blocks`.

    Chat-history persistence may store Thinking on completed replies for UI reload,
    but it must not enter later provider payloads (token bloat / stale signatures).
    Rows left empty by stripping are dropped.
    """
    out: list[Message] = []
    for msg in messages:
        kept = provider_visible_blocks(msg)
        if not kept and msg.content:
            logger.debug(
                "transform_context drop_non_provider_turn %s",
                kv(action="drop_non_provider_turn", role=msg.role, blocks=len(msg.content)),
            )
            continue
        out.append(msg if len(kept) == len(msg.content) else replace(msg, content=kept))
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
            out[-1] = replace(prev, content=[*prev.content, *msg.content])
        else:
            out.append(msg)
    return out


def transform_context(messages: Sequence[Message]) -> list[Message]:
    """Agent-facing history → cleaned message list for conversion.

    Steps:
    1. Strip UI-only blocks and completed-reply Thinking (see ``provider_visible_blocks``).
    2. Coalesce adjacent same-role messages left by dropped UI-only turns.
    3. Repair tool-call / tool-result pairing on that provider-shaped list.
    4. Coalesce again so a synthetic repair row is not left beside the same role.

    Repair runs after the strip. A UI-only row between a tool call and its
    result used to look like a missing result, so repair invented a second
    ``ToolResponse`` and the later coalesce merged it with the real one.
    """
    visible = _coalesce_adjacent_same_role(_strip_non_provider_blocks(list(messages)))
    return _coalesce_adjacent_same_role(repair_tool_turn_integrity(visible))
