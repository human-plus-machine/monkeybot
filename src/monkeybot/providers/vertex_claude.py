"""Anthropic Claude on Vertex AI (ADC)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.content_blocks import Text
from monkeybot.core.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolDef,
    UsageEvent,
)
from monkeybot.providers._utils import build_anthropic_messages

_log = logging.getLogger(__name__)


class VertexClaudeProvider:
    """Claude on Vertex AI — ADC, no ``ANTHROPIC_API_KEY``."""

    @property
    def name(self) -> str:
        return "vertex-claude"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        project_id: str | None = None,
        region: str | None = None,
    ) -> None:
        self._project_id = (
            (project_id or "").strip()
            or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
        )
        self._region = (
            (region or "").strip()
            or os.environ.get("ANTHROPIC_VERTEX_REGION", "").strip()
            or "global"
        )
        if not self._project_id:
            raise ValueError("ANTHROPIC_VERTEX_PROJECT_ID is not set (or pass project_id=)")

    def _convert_tools(self, tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        import anthropic  # noqa: PLC0415
        from anthropic import AsyncAnthropicVertex  # noqa: PLC0415

        msgs = list(messages)
        system = ""
        if msgs and msgs[0].role == "system":
            sys_msg = msgs[0]
            system = "\n\n".join(b.text for b in sys_msg.content if isinstance(b, Text))
            msgs = msgs[1:]

        converted_messages = build_anthropic_messages(msgs)
        client = AsyncAnthropicVertex(project_id=self._project_id, region=self._region)
        converted_tools = self._convert_tools(tools) if tools else anthropic.NOT_GIVEN

        _tool_input_buf = ""
        _tool_id = ""
        _tool_name = ""
        input_tokens = 0
        output_tokens = 0

        try:
            async with client.messages.stream(
                model=model,
                system=system or anthropic.NOT_GIVEN,
                messages=converted_messages,
                tools=converted_tools,
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
                                output_tokens = event.usage.output_tokens or 0
                        case "message_start":
                            if hasattr(event, "message") and event.message.usage:
                                input_tokens = event.message.usage.input_tokens or 0
        except Exception as exc:
            _log.warning("Vertex Claude stream error: %s", exc)
            raise

        yield UsageEvent(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=0,
        )
        yield Done()
