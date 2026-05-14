"""
Gemini provider — the default model backend.

Direct google-genai SDK. No LangChain. No LangGraph.
Implements the Provider Protocol from core/provider.py.

Lazy import: google.genai is only imported inside stream().
This keeps cold start fast — the SDK is not loaded until first use.

SDK version confirmed: google.genai (new SDK, >= 1.x).
google.generativeai is NOT installed; google.genai works.
API pattern: genai.Client(api_key=...) + client.aio.models.generate_content_stream().
"""
from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import ulid

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
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-2.0-flash-lite": (0.0375, 0.15),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
}


class GeminiProvider:
    """Direct Gemini provider using the google-genai SDK (new API)."""

    @property
    def name(self) -> str:
        """Unique provider identifier."""
        return "gemini"

    @property
    def supports_streaming(self) -> bool:
        """Whether this provider supports streaming responses."""
        return True

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert Message objects to google-genai Content dicts.

        Args:
            messages: Conversation history in provider-agnostic format.

        Returns:
            List of Content dicts suitable for the google-genai SDK.
        """
        contents: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "name": m.tool_name or "",
                                    "response": {"result": m.content},
                                }
                            }
                        ],
                    }
                )
            elif m.role == "assistant":
                contents.append({"role": "model", "parts": [{"text": m.content}]})
            else:
                contents.append({"role": "user", "parts": [{"text": m.content}]})
        return contents

    def _convert_tools(self, tools: list[ToolDef]) -> list[dict[str, Any]]:
        """Convert ToolDef objects to google-genai Tool dicts.

        Args:
            tools: Tool definitions in provider-agnostic format.

        Returns:
            List of Tool dicts suitable for the google-genai SDK.
        """
        return [
            {
                "function_declarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                ]
            }
            for t in tools
        ]

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str = "gemini-2.0-flash",
        system: str = "",
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream model responses as ProviderEvent objects.

        Lazy-imports google.genai inside this method to preserve the 200ms
        cold-start budget — the SDK itself takes ~150ms to import.

        Args:
            messages: Conversation history.
            tools: Tool definitions available to the model.
            model: Model identifier string.
            system: System prompt text.
            context: Optional turn context (unused by this provider directly).

        Yields:
            TextDelta for each text chunk, ToolCall for each function call,
            and always ProviderDone as the final event.
        """
        # Lazy import — MUST stay inside method to preserve cold-start budget.
        import google.genai as genai  # noqa: PLC0415
        import google.genai.types as gtypes  # noqa: PLC0415

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        contents = self._convert_messages(messages)

        config = gtypes.GenerateContentConfig(
            system_instruction=system or None,
            tools=self._convert_tools(tools) if tools else None,  # type: ignore[arg-type]
            automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(
                disable=True
            )
            if tools
            else None,
        )

        input_tokens = 0
        output_tokens = 0
        try:
            response_iter = await client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            async for chunk in response_iter:
                candidates = chunk.candidates or []
                if candidates and candidates[0].content:
                    for part in candidates[0].content.parts or []:
                        if part.text:
                            yield TextDelta(text=part.text)
                        if part.function_call:
                            fc = part.function_call
                            yield ToolCall(
                                call_id=str(ulid.new()),
                                name=fc.name or "",
                                args=dict(fc.args) if fc.args else {},
                            )
                if chunk.usage_metadata:
                    um = chunk.usage_metadata
                    input_tokens = um.prompt_token_count or 0
                    output_tokens = (
                        getattr(um, "response_token_count", None) or 0
                    )
        except Exception as exc:
            _log.warning("Gemini stream error: %s", exc)

        yield ProviderDone(
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=estimate_cost(model, input_tokens, output_tokens, _PRICING),
            )
        )
