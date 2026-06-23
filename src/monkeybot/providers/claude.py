"""Anthropic Claude provider (direct API, async streaming)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    build_anthropic_messages,
    build_cached_system_blocks,
    mark_last_tool_cached,
)

_log = logging.getLogger(__name__)


class ClaudeProvider:
    """Anthropic Claude using the ``anthropic`` SDK."""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(self, *, cache_enabled: bool = True) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._cache_enabled = cache_enabled

    def _convert_tools(self, tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> int:
        import anthropic  # noqa: PLC0415

        msgs = list(messages)
        system = ""
        if msgs and msgs[0].role == "system":
            sys_msg = msgs[0]
            system = "\n\n".join(b.text for b in sys_msg.content if isinstance(b, Text))
            msgs = msgs[1:]

        converted_messages = build_anthropic_messages(msgs)
        converted_tools = self._convert_tools(tools) if tools else anthropic.NOT_GIVEN

        client = anthropic.AsyncAnthropic()
        resp = await client.messages.count_tokens(
            model=model,
            system=cast(Any, system or anthropic.NOT_GIVEN),
            messages=cast(Any, converted_messages),
            tools=cast(Any, converted_tools),
        )
        return int(resp.input_tokens)

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        import anthropic  # noqa: PLC0415

        msgs = list(messages)
        system = ""
        if msgs and msgs[0].role == "system":
            sys_msg = msgs[0]
            system = "\n\n".join(b.text for b in sys_msg.content if isinstance(b, Text))
            msgs = msgs[1:]

        converted_messages = build_anthropic_messages(msgs)
        converted = self._convert_tools(tools) if tools else None
        if self._cache_enabled and system:
            system_param: Any = build_cached_system_blocks(system)
        else:
            system_param = system or anthropic.NOT_GIVEN

        if self._cache_enabled and converted:
            tools_param: Any = mark_last_tool_cached(converted)
        else:
            tools_param = converted if converted else anthropic.NOT_GIVEN

        client = anthropic.AsyncAnthropic()
        _tool_input_buf = ""
        _tool_id = ""
        _tool_name = ""
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_creation = 0

        try:
            async with client.messages.stream(
                model=model,
                system=system_param,
                messages=cast(Any, converted_messages),
                tools=tools_param,
                max_tokens=4096,
            ) as stream:
                async for event in stream:
                    match event.type:
                        case "content_block_start":
                            if event.content_block.type == "tool_use":
                                _tool_id = event.content_block.id
                                _tool_name = event.content_block.name
                                _tool_input_buf = ""
                        case "content_block_delta":
                            if event.delta.type == "text_delta":
                                yield TextDelta(text=event.delta.text)
                            elif event.delta.type == "input_json_delta":
                                _tool_input_buf += event.delta.partial_json
                        case "content_block_stop":
                            if _tool_id:
                                yield ToolCall(
                                    call_id=_tool_id,
                                    name=_tool_name,
                                    args=json.loads(_tool_input_buf or "{}"),
                                )
                                _tool_id = _tool_name = _tool_input_buf = ""
                        case "message_delta":
                            if hasattr(event, "usage"):
                                output_tokens = int(
                                    getattr(event.usage, "output_tokens", 0) or 0
                                )
                                _r = int(
                                    getattr(event.usage, "cache_read_input_tokens", 0)
                                    or 0
                                )
                                _c = int(
                                    getattr(
                                        event.usage, "cache_creation_input_tokens", 0
                                    )
                                    or 0
                                )
                                if _r:
                                    cache_read = _r
                                if _c:
                                    cache_creation = _c
                        case "message_start":
                            if hasattr(event, "message") and event.message.usage:
                                usage = event.message.usage
                                input_tokens = int(
                                    getattr(usage, "input_tokens", 0) or 0
                                )
                                cache_read = int(
                                    getattr(usage, "cache_read_input_tokens", 0) or 0
                                )
                                cache_creation = int(
                                    getattr(usage, "cache_creation_input_tokens", 0)
                                    or 0
                                )
        except Exception as exc:
            _log.warning("Claude stream error: %s", exc)
            raise

        yield UsageEvent(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            cached_tokens=cache_read + cache_creation,
        )
        yield Done()
