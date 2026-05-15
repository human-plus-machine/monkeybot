"""OpenAI Chat Completions provider (direct ``openai`` SDK, async streaming)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.content_blocks import ContentBlock, Text, ToolRequest, ToolResponse
from monkeybot.core.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolDef,
    UsageEvent,
)

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
                f"unsupported ToolResponse block for OpenAI: {type(b).__name__}"
            )
    return "".join(parts)


def _messages_to_openai(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, Any]]]:
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
                    f"unsupported user content block for OpenAI: {type(item).__name__}"
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
                        f"unsupported assistant block for OpenAI: {type(block).__name__}"
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
            raise ValueError(f"unsupported role for OpenAI: {m.role!r}")

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


def _openai_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
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


class OpenAIProvider:
    """OpenAI chat models using the official ``openai`` async client."""

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set")

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        from openai import AsyncOpenAI  # noqa: PLC0415

        msgs = list(messages)
        temperature = float(os.environ.get("MODEL_TEMPERATURE", "0.7"))
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", "4096"))

        system, oai_messages = _messages_to_openai(msgs)
        if system:
            oai_messages = [{"role": "system", "content": system}, *oai_messages]

        client = AsyncOpenAI()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "stream": True,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = _openai_tools(tools)
            kwargs["parallel_" + "tool" + "_calls"] = True
        # ``max_tokens`` vs ``max_completion_tokens`` — older models use max_tokens
        kwargs["max_tokens"] = max_tokens

        input_tokens = 0
        output_tokens = 0
        tool_buf: dict[int, dict[str, Any]] = {}

        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if chunk.usage is not None:
                    input_tokens = int(chunk.usage.prompt_tokens or 0)
                    output_tokens = int(chunk.usage.completion_tokens or 0)
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
                try:
                    parsed: dict[str, object] = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed = {}
                yield ToolCall(call_id=tid, name=name, args=parsed)
        except Exception as exc:
            _log.warning("OpenAI stream error: %s", exc)

        yield UsageEvent(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=0,
        )
        yield Done()
