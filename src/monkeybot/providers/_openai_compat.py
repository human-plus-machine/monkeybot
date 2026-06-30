"""Shared helpers for OpenAI-compatible Chat Completions providers.

Used by OpenAIProvider and HuggingFaceProvider — same wire format, different auth/URLs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import safe_parse_tool_args

_log = logging.getLogger(__name__)


def _system_prompt_from_message(message: Message) -> str:
    texts = [b.text for b in message.content if isinstance(b, Text)]
    return "\n\n".join(texts)


def _flatten_tool_response_text(block: ToolResponse) -> str:
    parts: list[str] = []
    for b in block.result:
        if isinstance(b, Text):
            parts.append(b.text)
        else:
            raise ValueError(
                f"unsupported ToolResponse block for OpenAI-compat: {type(b).__name__}"
            )
    return "".join(parts)


def messages_to_openai(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """Split system prompt text vs OpenAI Chat messages (block-native)."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def flush_user_blocks(buf: list[ContentBlock]) -> None:
        if not buf:
            return
        if len(buf) == 1 and isinstance(buf[0], Text):
            out.append({"role": "user", "content": buf[0].text})
            return
        content: list[dict[str, Any]] = []
        for item in buf:
            if isinstance(item, Text):
                content.append({"type": "text", "text": item.text})
            else:
                raise ValueError(
                    f"unsupported user content block for OpenAI-compat: {type(item).__name__}"
                )
        out.append({"role": "user", "content": content})

    for m in messages:
        if m.role == "system":
            sys_txt = _system_prompt_from_message(m)
            if sys_txt.strip():
                system_parts.append(sys_txt)
            continue

        if m.role == "assistant":
            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in m.content:
                if isinstance(block, Text):
                    text_chunks.append(block.text)
                elif isinstance(block, ToolRequest):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(dict(block.args), ensure_ascii=False),
                            },
                        }
                    )
                else:
                    raise ValueError(
                        f"unsupported assistant block for OpenAI-compat: {type(block).__name__}"
                    )
            row: dict[str, Any] = {"role": "assistant"}
            if len(text_chunks) == 1:
                row["content"] = text_chunks[0]
            elif len(text_chunks) > 1:
                row["content"] = "\n\n".join(text_chunks)
            elif not tool_calls:
                row["content"] = None
            else:
                row["content"] = None
            if tool_calls:
                row["tool_calls"] = tool_calls
            out.append(row)
            continue

        if m.role != "user":
            raise ValueError(f"unsupported role for OpenAI-compat: {m.role!r}")

        buf: list[ContentBlock] = []
        for block in m.content:
            if isinstance(block, ToolResponse):
                flush_user_blocks(buf)
                buf.clear()
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.id,
                        "content": _flatten_tool_response_text(block),
                    }
                )
            else:
                buf.append(block)
        flush_user_blocks(buf)

    joined_system = "\n\n".join(system_parts).strip()
    return (joined_system or None, out)


def openai_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def openai_messages_token_count(encoding: Any, oai_messages: list[dict[str, Any]]) -> int:
    """tiktoken-based estimate for Chat Completions-shaped message dicts."""
    total = 0
    for m in oai_messages:
        total += 4
        for key, val in m.items():
            if val is None:
                continue
            if key == "tool_calls" and isinstance(val, list):
                for tc in val:
                    total += len(
                        encoding.encode(json.dumps(tc, ensure_ascii=False, default=str))
                    )
            elif isinstance(val, str):
                total += len(encoding.encode(val))
            else:
                total += len(encoding.encode(json.dumps(val, ensure_ascii=False, default=str)))
    total += 2
    return total


def openai_tools_token_count(encoding: Any, tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    return len(encoding.encode(json.dumps(tools, ensure_ascii=False, default=str)))


def count_openai_compat_input_tokens(
    encoding: Any,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
) -> int:
    system, oai_messages = messages_to_openai(messages)
    if system:
        oai_messages = [{"role": "system", "content": system}, *oai_messages]
    tool_defs = openai_tools(tools) if tools else []
    return openai_messages_token_count(encoding, oai_messages) + openai_tools_token_count(
        encoding, tool_defs
    )


async def iter_openai_compat_stream(
    client: Any,
    kwargs: dict[str, Any],
    *,
    provider: str = "openai_compat",
    n_messages: int | None = None,
    n_tools: int | None = None,
) -> AsyncIterator[ProviderEvent]:
    """Yield ProviderEvents from an OpenAI-compatible streaming chat request.

    Handles text deltas, tool call buffering, usage accounting, and Done.
    ``client`` must be an ``AsyncOpenAI``-compatible object.
    """
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    tool_buf: dict[int, dict[str, Any]] = {}

    try:
        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if chunk.usage is not None:
                input_tokens = int(chunk.usage.prompt_tokens or 0)
                output_tokens = int(chunk.usage.completion_tokens or 0)
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                yield TextDelta(text=delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = int(tc.index or 0)
                    slot = tool_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["args"] += tc.function.arguments

        for slot in tool_buf.values():
            name = str(slot.get("name") or "")
            if not name:
                continue
            tid = str(slot.get("id") or "") or f"anon:{name}"
            raw_args = str(slot.get("args") or "{}")
            parsed, parse_error = safe_parse_tool_args(
                raw_args,
                call_id=tid,
                tool_name=name,
                provider="openai_compat",
            )
            yield ToolCall(call_id=tid, name=name, args=parsed, parse_error=parse_error)
    except Exception:
        _log.warning(
            "OpenAI-compat stream error %s",
            kv(
                provider=provider,
                model=kwargs.get("model"),
                n_messages=n_messages,
                n_tools=n_tools,
            ),
            exc_info=True,
        )
        raise

    # OpenAI reports prompt_tokens incl. cached; subtract to avoid double-count (see 1C).
    yield UsageEvent(
        input_tokens=max(0, input_tokens - cached_tokens),
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_read_tokens=cached_tokens,
        cache_creation_tokens=0,
    )
    yield Done()


def is_tool_unsupported_error(exc: BaseException) -> bool:
    """Return True when the exception likely indicates unsupported tool calling.

    Shared by providers (HuggingFace, Ollama, …) that fall back to a tool-less
    request when the upstream model/server rejects function calling.
    """
    try:
        from openai import APIStatusError  # noqa: PLC0415
    except ImportError:
        msg = str(exc).lower()
        return "tool" in msg and "unsupported" in msg

    if isinstance(exc, APIStatusError):
        if exc.status_code not in (400, 422):
            return False
        body = str(exc.body or exc.message or "").lower()
        return any(
            phrase in body
            for phrase in (
                "tool",
                "function_call",
                "functions",
                "unsupported",
            )
        )

    msg = str(exc).lower()
    return "tool" in msg and "unsupported" in msg


__all__ = [
    "count_openai_compat_input_tokens",
    "is_tool_unsupported_error",
    "iter_openai_compat_stream",
    "messages_to_openai",
    "openai_messages_token_count",
    "openai_tools",
    "openai_tools_token_count",
]
