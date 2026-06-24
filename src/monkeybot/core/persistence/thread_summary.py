"""Shared helpers for listing persisted chat threads."""

from __future__ import annotations

import json
from dataclasses import dataclass

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text


@dataclass(frozen=True)
class ChatThreadSummary:
    """One row in a chat-thread picker."""

    thread_id: str
    last_message_at: int
    message_count: int
    preview: str


def preview_from_content_blob(content_blob: str, *, max_len: int = 120) -> str:
    """Build a one-line preview from stored JSON content blocks."""
    try:
        raw = json.loads(content_blob)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    joined = " ".join(parts).replace("\n", " ").strip()
    if len(joined) <= max_len:
        return joined
    return joined[: max_len - 1].rstrip() + "…"


def text_from_message(message: Message) -> str:
    """Concatenate Text blocks from a stored message."""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, Text) and block.text.strip():
            parts.append(block.text.strip())
    return "\n".join(parts).strip()


def messages_to_playground_wire(messages: list[Message]) -> list[dict[str, str]]:
    """Serialize user/assistant turns for the playground chat UI."""
    out: list[dict[str, str]] = []
    for msg in messages:
        if msg.role not in ("user", "assistant"):
            continue
        text = text_from_message(msg)
        if not text:
            continue
        out.append({"role": msg.role, "text": text})
    return out
