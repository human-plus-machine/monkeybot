"""Anthropic Claude on Vertex AI provider.

Uses Google Application Default Credentials (ADC) — no ANTHROPIC_API_KEY needed.
Set GOOGLE_APPLICATION_CREDENTIALS to your service account JSON before running.

Required env vars:
  ANTHROPIC_VERTEX_PROJECT_ID   GCP project ID
  ANTHROPIC_VERTEX_REGION       Vertex region (default: "global")

Install: pip install "anthropic[vertex]"

Model IDs (as of 2026 — check Vertex AI Model Garden for latest):
  claude-opus-4-7
  claude-sonnet-4-5@20250929
  claude-haiku-4-5@20251001
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.provider import (
    Message,
    ProviderDone,
    ProviderEvent,
    ProviderUsage,
    TextDelta,
    ToolCall,
    ToolDef,
)
from monkeybot.providers._utils import estimate_cost

_log = logging.getLogger(__name__)

# Vertex AI pricing matches direct Anthropic API (global endpoint, pay-as-you-go)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-6@default": (3.00, 15.00),
    "claude-sonnet-4-5@20250929": (3.00, 15.00),
    "claude-haiku-4-5@20251001": (0.80, 4.00),
    "claude-opus-4-5@20251101": (15.00, 75.00),
    "claude-opus-4-1@20250805": (15.00, 75.00),
}


class VertexClaudeProvider:
    """Claude on Vertex AI — ADC auth, no Anthropic API key required."""

    @property
    def name(self) -> str:
        return "vertex-claude"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(self) -> None:
        self._project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
        self._region = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
        if not self._project_id:
            raise ValueError("ANTHROPIC_VERTEX_PROJECT_ID is not set")

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role == "tool":
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content,
                    }],
                })
            elif m.role == "assistant" and m.tool_call_id:
                tool_name = ""
                if i + 1 < len(messages) and messages[i + 1].role == "tool":
                    tool_name = messages[i + 1].tool_name or ""
                result.append({
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": m.tool_call_id,
                        "name": tool_name,
                        "input": {},
                    }],
                })
            elif m.role == "assistant":
                result.append({"role": "assistant", "content": m.content})
            else:
                result.append({"role": "user", "content": m.content})
            i += 1
        return result

    def _convert_tools(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str = "claude-sonnet-4-5@20250929",
        system: str = "",
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream Claude responses via Vertex AI using ADC.

        Lazy-imports anthropic[vertex] to preserve cold-start budget.
        """
        import json  # noqa: PLC0415

        from anthropic import AsyncAnthropicVertex  # noqa: PLC0415

        converted_messages = self._convert_messages(messages)

        async def _generate() -> AsyncIterator[ProviderEvent]:
            import anthropic  # noqa: PLC0415

            client = AsyncAnthropicVertex(
                project_id=self._project_id,
                region=self._region,
            )
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

            yield ProviderDone(
                usage=ProviderUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=estimate_cost(model, input_tokens, output_tokens, _PRICING),
                )
            )

        return _generate()
