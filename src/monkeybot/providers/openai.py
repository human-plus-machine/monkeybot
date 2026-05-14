"""OpenAI Chat Completions provider (direct ``openai`` SDK, async streaming)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

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


def _split_assistant_placeholder(content: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse trailing ``{"tool_calls": [...]}`` from a stored assistant row (same shape as loop)."""
    last_nl = content.rfind("\n")
    if last_nl == -1:
        return content, []
    tail = content[last_nl + 1 :].strip()
    try:
        obj = json.loads(tail)
    except json.JSONDecodeError:
        return content, []
    tc = obj.get("tool_calls")
    if not isinstance(tc, list):
        return content, []
    head = content[:last_nl]
    return head, [x for x in tc if isinstance(x, dict)]


def _messages_to_openai(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """Return ``(system_text_or_none, openai_chat_messages)``."""
    systems: list[str] = []
    rest: list[Message] = []
    for m in messages:
        if m.role == "system":
            systems.append(m.content)
        else:
            rest.append(m)
    system = "\n\n".join(systems).strip() or None

    out: list[dict[str, Any]] = []
    for m in rest:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            text, raw_calls = _split_assistant_placeholder(m.content)
            if not raw_calls:
                out.append({"role": "assistant", "content": m.content})
                continue
            tool_calls: list[dict[str, Any]] = []
            for tc in raw_calls:
                cid = str(tc.get("call_id") or tc.get("id") or "")
                name = str(tc.get("name") or "")
                args = tc.get("args")
                arg_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else "{}"
                tool_calls.append(
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": name, "arguments": arg_str},
                    }
                )
            row: dict[str, Any] = {
                "role": "assistant",
                "content": text if text.strip() else None,
                "tool_calls": tool_calls,
            }
            out.append(row)
        elif m.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content,
                }
            )
    return system, out


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
            kwargs["parallel_tool_calls"] = True
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
