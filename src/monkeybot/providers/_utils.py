"""Shared provider utilities (cost estimation, Anthropic message shaping)."""

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
    ThinkingDelta,
    ToolCall,
    ToolInputDelta,
    UsageEvent,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    File,
    Image,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
from monkeybot.providers.pricing import estimate_cost

_log = logging.getLogger(__name__)


def safe_parse_tool_args(
    raw: str,
    *,
    call_id: str,
    tool_name: str,
    provider: str,
) -> tuple[dict[str, object], str | None]:
    """Parse streamed tool arguments; return ``({}, error)`` when JSON is invalid."""
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        action = "malformed_tool_args"
        error = f"malformed tool args JSON: {exc}"
    else:
        if isinstance(parsed, dict):
            return parsed, None
        action = "non_object_tool_args"
        error = f"tool args must be a JSON object, got {type(parsed).__name__}"
    _log.warning(
        "stream_parse_repair %s",
        kv(
            action=action,
            call_id=call_id,
            tool_name=tool_name,
            provider=provider,
        ),
    )
    return {}, error


def anthropic_tool_defs(tools: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def split_leading_system(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    msgs = list(messages)
    if not msgs or msgs[0].role != "system":
        return "", msgs
    system = "\n\n".join(b.text for b in msgs[0].content if isinstance(b, Text))
    return system, msgs[1:]


async def count_anthropic_input_tokens(
    client: Any,
    *,
    anthropic_module: Any,
    model: str,
    system: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
) -> int:
    resp = await client.messages.count_tokens(
        model=model,
        system=system or anthropic_module.NOT_GIVEN,
        messages=messages,
        tools=tools if tools else anthropic_module.NOT_GIVEN,
    )
    return int(resp.input_tokens)


async def iter_anthropic_sdk_stream(
    client: Any,
    stream_kwargs: dict[str, Any],
    *,
    provider: str,
    error_message: str,
    n_messages: int | None = None,
    n_tools: int | None = None,
) -> AsyncIterator[ProviderEvent]:
    tool_input_buf = ""
    tool_id = ""
    tool_name = ""
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0
    stop_reason: str | None = None

    try:
        async with client.messages.stream(**stream_kwargs) as stream:
            async for event in stream:
                match event.type:
                    case "content_block_start":
                        if event.content_block.type == "tool_use":
                            tool_id = event.content_block.id
                            tool_name = event.content_block.name
                            tool_input_buf = ""
                    case "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield TextDelta(text=event.delta.text)
                        elif event.delta.type == "thinking_delta":
                            thought = getattr(event.delta, "thinking", None) or ""
                            if thought:
                                yield ThinkingDelta(text=thought)
                        elif event.delta.type == "signature_delta":
                            sig = getattr(event.delta, "signature", None) or ""
                            if sig:
                                yield ThinkingDelta(text="", signature=sig)
                        elif event.delta.type == "input_json_delta":
                            partial = event.delta.partial_json
                            tool_input_buf += partial
                            if tool_id and partial:
                                yield ToolInputDelta(
                                    call_id=tool_id,
                                    name=tool_name,
                                    delta=partial,
                                )
                    case "content_block_stop":
                        if tool_id:
                            args, parse_error = safe_parse_tool_args(
                                tool_input_buf,
                                call_id=tool_id,
                                tool_name=tool_name,
                                provider=provider,
                            )
                            yield ToolCall(
                                call_id=tool_id,
                                name=tool_name,
                                args=args,
                                parse_error=parse_error,
                            )
                            tool_id = tool_name = tool_input_buf = ""
                    case "message_delta":
                        delta = getattr(event, "delta", None)
                        sr = getattr(delta, "stop_reason", None) if delta is not None else None
                        if sr:
                            stop_reason = str(sr)
                        if hasattr(event, "usage"):
                            output_tokens = int(getattr(event.usage, "output_tokens", 0) or 0)
                            read = int(
                                getattr(event.usage, "cache_read_input_tokens", 0) or 0
                            )
                            created = int(
                                getattr(event.usage, "cache_creation_input_tokens", 0) or 0
                            )
                            if read:
                                cache_read = read
                            if created:
                                cache_creation = created
                    case "message_start":
                        if hasattr(event, "message") and event.message.usage:
                            usage = event.message.usage
                            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                            cache_read = int(
                                getattr(usage, "cache_read_input_tokens", 0) or 0
                            )
                            cache_creation = int(
                                getattr(usage, "cache_creation_input_tokens", 0) or 0
                            )
    except Exception:
        _log.warning(
            error_message,
            kv(
                provider=provider,
                model=stream_kwargs.get("model"),
                n_messages=n_messages,
                n_tools=n_tools,
            ),
            exc_info=True,
        )
        raise

    yield UsageEvent(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cached_tokens=cache_read + cache_creation,
    )
    yield Done(truncated=stop_reason == "max_tokens")


def _anthropic_tool_result_content(result: list[ContentBlock]) -> list[dict[str, Any]]:
    """Map ``ToolResponse.result`` blocks to Anthropic ``tool_result`` content list."""
    out: list[dict[str, Any]] = []
    for block in result:
        if isinstance(block, Text):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type,
                        "data": block.data,
                    },
                }
            )
        elif isinstance(block, File):
            out.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": block.mime_type,
                        "data": block.data,
                    },
                }
            )
        else:
            raise ValueError(
                f"unsupported ToolResponse block for Anthropic: {type(block).__name__}"
            )
    return out


def _anthropic_user_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, Image):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.mime_type,
                "data": block.data,
            },
        }
    if isinstance(block, File):
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": block.mime_type,
                "data": block.data,
            },
        }
    if isinstance(block, ToolResponse):
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": _anthropic_tool_result_content(block.result),
        }
    raise ValueError(f"unsupported user content block for Anthropic: {type(block).__name__}")


def _anthropic_assistant_block(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, Text):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolRequest):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.args),
        }
    if isinstance(block, Thinking):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    if isinstance(block, RedactedThinking):
        return {"type": "redacted_thinking", "data": block.data}
    raise ValueError(
        f"unsupported assistant content block for Anthropic: {type(block).__name__}"
    )


# Must match volatile section headers emitted by ``compose_system_prompt``.
_VOLATILE_SYSTEM_MARKERS = (
    "\n\n## Memory index\n",
    "\n\n## Memory\n",
    "\n\n## Skills\n",
    "\n\n## Current request\n",
    "\n\n## Runtime notes\n",
    "\n\n## System context update\n",
)


def split_system_prompt_for_cache(system: str) -> tuple[str, str]:
    """Split composed system text into stable (cacheable) prefix and volatile tail."""
    split_at = len(system)
    for marker in _VOLATILE_SYSTEM_MARKERS:
        idx = system.find(marker)
        if idx != -1:
            split_at = min(split_at, idx)
    if split_at >= len(system):
        return system, ""
    return system[:split_at], system[split_at:]


def build_cached_system_blocks(system: str) -> list[dict[str, Any]]:
    """Return Anthropic system blocks with cache_control only on the stable prefix.

    Volatile tail sections (memory, skills, current request) are sent in a second
    uncached block so explicit caching hits across curation turns.

    Args:
        system: Non-empty system prompt text. Callers MUST guard empty strings
            and pass anthropic.NOT_GIVEN instead (see provider stream methods).

    Returns:
        One or two text blocks; the stable prefix carries ``cache_control: ephemeral``.
    """
    stable, volatile = split_system_prompt_for_cache(system)
    if not volatile.strip():
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
    ]
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks


def mark_last_tool_cached(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``tools`` with ``cache_control: ephemeral`` on the LAST tool.

    Marks the final tool dict so Anthropic caches the entire tools-array prefix.
    No-ops (returns the list unchanged in content) when ``tools`` is empty.

    Args:
        tools: Anthropic tool dicts (output of a provider ``_convert_tools``).

    Returns:
        A new list; only the last element gains a ``cache_control`` key. Input
        list and its dicts are not mutated (shallow-copy the last dict).
    """
    if not tools:
        return tools
    marked_last = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return [*tools[:-1], marked_last]


def build_anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert harness messages to Anthropic ``messages`` API shape (no string parsing)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role == "tool":
            raise ValueError('disallowed role "tool"; use user messages with ToolResponse blocks')
        if role not in ("user", "assistant"):
            raise ValueError(f"unsupported role for Anthropic messages: {role!r}")

        blocks = list(m.content)

        if role == "user":
            if len(blocks) == 1 and isinstance(blocks[0], Text):
                out.append({"role": "user", "content": blocks[0].text})
                continue
            out.append(
                {
                    "role": "user",
                    "content": [_anthropic_user_block(b) for b in blocks],
                }
            )
            continue

        # assistant
        if len(blocks) == 1 and isinstance(blocks[0], Text):
            out.append({"role": "assistant", "content": blocks[0].text})
            continue
        out.append(
            {
                "role": "assistant",
                "content": [_anthropic_assistant_block(b) for b in blocks],
            }
        )
    return out


def estimate_anthropic_input_tokens(
    *,
    system: str,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
) -> int:
    """Rough prompt token estimate when provider ``count_tokens`` is unavailable (e.g. Bedrock)."""
    parts: list[str] = [system]
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.append(json.dumps(content, ensure_ascii=False))
        else:
            parts.append(str(content))
    if tools:
        parts.append(json.dumps(list(tools), ensure_ascii=False))
    char_count = sum(len(p) for p in parts)
    return max(1, char_count // 4)


__all__ = [
    "anthropic_tool_defs",
    "build_anthropic_messages",
    "build_cached_system_blocks",
    "count_anthropic_input_tokens",
    "estimate_anthropic_input_tokens",
    "estimate_cost",
    "iter_anthropic_sdk_stream",
    "mark_last_tool_cached",
    "safe_parse_tool_args",
    "split_leading_system",
    "split_system_prompt_for_cache",
]
