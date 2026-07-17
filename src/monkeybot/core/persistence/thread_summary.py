"""Shared helpers for listing persisted chat threads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)


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


def _text_from_blocks(blocks: list[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text) and block.text.strip():
            parts.append(block.text.strip())
    return "\n".join(parts).strip()


def _tool_wire_title(name: str, args: dict[str, object]) -> str:
    """Short UI title; mirrors CLI ``tool_collapsed_title`` without importing CLI."""
    kind_map = {
        "run_command": "Shell",
        "execute": "Shell",
        "shell": "Shell",
        "bash": "Shell",
        "read_file": "Read",
        "read": "Read",
        "write_file": "Write",
        "write": "Write",
        "edit_file": "Edit",
        "apply_patch": "Edit",
        "str_replace": "Edit",
        "grep": "Search",
        "search": "Search",
        "web_search": "Search",
        "glob": "Glob",
        "list_dir": "List",
        "list_directory": "List",
        "task": "Task",
    }
    key = name.strip().lower().replace("-", "_")
    kind = kind_map.get(key)
    if kind is None:
        cleaned = name.strip().replace("_", " ").replace("-", " ")
        kind = cleaned.title() if cleaned else "Tool"

    hint = ""
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        hint = " ".join(str(x) for x in argv)
    else:
        for field in ("command", "shell", "script", "path", "query", "url", "task"):
            val = args.get(field)
            if isinstance(val, str) and val.strip():
                hint = val.strip()
                break
    hint = " ".join(hint.split())
    if len(hint) > 72:
        hint = hint[:72] + "…"
    return f"{kind}  {hint}" if hint else kind


def _tool_responses_by_id(messages: list[Message]) -> dict[str, ToolResponse]:
    out: dict[str, ToolResponse] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolResponse) and block.id:
                out[block.id] = block
    return out


def messages_to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize turns for the chat-history API.

    Assistant messages expand into multiple wire rows when they contain
    thinking / tool-request blocks so UIs can restore the same traces that
    were streamed live over SSE:

    - ``role=thinking`` before assistant text
    - ``role=tool`` for each ``ToolRequest`` (with matching ``ToolResponse``)
    - ``role=assistant`` for Text
    - ``role=user`` for user Text (tool-result-only user rows are omitted)
    """
    responses = _tool_responses_by_id(messages)
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            text = text_from_message(msg)
            if text:
                out.append({"role": "user", "text": text})
            continue
        if msg.role != "assistant":
            continue

        thinking_parts: list[str] = []
        text_parts: list[str] = []
        tool_requests: list[ToolRequest] = []
        for block in msg.content:
            if isinstance(block, Thinking) and block.thinking.strip():
                thinking_parts.append(block.thinking.strip())
            elif isinstance(block, RedactedThinking):
                thinking_parts.append("(redacted thinking)")
            elif isinstance(block, Text) and block.text.strip():
                text_parts.append(block.text.strip())
            elif isinstance(block, ToolRequest):
                tool_requests.append(block)

        for thinking in thinking_parts:
            out.append({"role": "thinking", "text": thinking})

        text = "\n".join(text_parts).strip()
        if text:
            out.append({"role": "assistant", "text": text})

        for req in tool_requests:
            resp = responses.get(req.id)
            result_text = _text_from_blocks(resp.result) if resp is not None else ""
            error: str | None = None
            if resp is not None and resp.is_error:
                error = result_text or "tool error"
                result_text = ""
            row: dict[str, Any] = {
                "role": "tool",
                "text": _tool_wire_title(req.name, dict(req.args)),
                "tool": req.name,
                "call_id": req.id,
                "args": dict(req.args),
            }
            if result_text:
                row["result"] = result_text
            if error:
                row["error"] = error
            out.append(row)
    return out
