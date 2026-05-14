"""Anthropic Claude provider.

Implements the Provider Protocol from core/provider.py.
Lazy import: anthropic is only imported inside stream().
Keeps cold start fast — the SDK is not loaded until first use.
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

_PRICING: dict[str, tuple[float, float]] = {
    # (input_per_million, output_per_million) in USD
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "claude-3-opus-20240229": (15.00, 75.00),
}


class ClaudeProvider:
    """Anthropic Claude provider using the anthropic SDK (async)."""

    @property
    def name(self) -> str:
        """Unique provider identifier."""
        return "claude"

    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming responses."""
        return True

    def __init__(self) -> None:
        """Initialize ClaudeProvider.

        Raises:
            ValueError: If ANTHROPIC_API_KEY environment variable is not set.
        """
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert Message objects to Anthropic API message dicts.

        Handles user, assistant, tool-call, and tool-result roles.
        Assistant tool_use blocks are paired with their following tool message
        to extract the tool name.

        Args:
            messages: Conversation history in provider-agnostic format.

        Returns:
            List of message dicts suitable for the Anthropic SDK.
        """
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            elif m.role == "assistant" and m.tool_call_id:
                # Peek at the next message to get the tool name
                tool_name = ""
                if i + 1 < len(messages) and messages[i + 1].role == "tool":
                    tool_name = messages[i + 1].tool_name or ""
                result.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": m.tool_call_id,
                                "name": tool_name,
                                "input": {},
                            }
                        ],
                    }
                )
            elif m.role == "assistant":
                result.append({"role": "assistant", "content": m.content})
            else:
                result.append({"role": "user", "content": m.content})
            i += 1
        return result

    def _convert_tools(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        """Convert ToolDef objects to Anthropic tool dicts.

        Args:
            tools: Tool definitions in provider-agnostic format.

        Returns:
            List of tool dicts suitable for the Anthropic SDK.
        """
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str = "claude-3-5-sonnet-20241022",
        system: str = "",
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream model responses as ProviderEvent objects.

        Lazy-imports anthropic inside this method to preserve the 200ms
        cold-start budget — the SDK itself takes ~80ms to import.

        Args:
            messages: Conversation history.
            tools: Tool definitions available to the model.
            model: Model identifier string.
            system: System prompt text.
            context: Optional turn context (unused by this provider directly).

        Returns:
            An async iterator of ProviderEvent objects. TextDelta for each text
            chunk, ToolCall for each function call, and always ProviderDone as
            the final event.
        """
        import json  # noqa: PLC0415

        import anthropic  # type: ignore[import-not-found]  # noqa: PLC0415

        converted_messages = self._convert_messages(messages)
        converted_tools = self._convert_tools(tools) if tools else anthropic.NOT_GIVEN

        async def _generate() -> AsyncIterator[ProviderEvent]:
            client = anthropic.AsyncAnthropic()
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
                _log.warning("Claude stream error: %s", exc)

            yield ProviderDone(
                usage=ProviderUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=estimate_cost(model, input_tokens, output_tokens, _PRICING),
                )
            )

        return _generate()
