"""Anthropic Claude on Vertex AI (ADC)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from monkeybot.core.llm.provider import Message, ProviderCallHints, ProviderEvent
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    anthropic_tool_defs,
    build_anthropic_messages,
    count_anthropic_input_tokens,
    estimate_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    prepare_anthropic_cached_payload,
    split_leading_system,
)
from monkeybot.providers.model_capabilities import supports_param
from monkeybot.providers.sampling import resolve_model_sampling

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
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        from monkeybot.core.config.snapshot import current_env

        self._project_id = (project_id or "").strip() or current_env(
            "ANTHROPIC_VERTEX_PROJECT_ID", ""
        ).strip()
        self._region = (
            (region or "").strip()
            or current_env("ANTHROPIC_VERTEX_REGION", "").strip()
            or "global"
        )
        if not self._project_id:
            raise ValueError("ANTHROPIC_VERTEX_PROJECT_ID is not set (or pass project_id=)")
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: ProviderCallHints | None = None,
    ) -> int:
        del thinking_budget, hints
        import anthropic  # noqa: PLC0415
        from anthropic import AsyncAnthropicVertex  # noqa: PLC0415

        system, msgs = split_leading_system(messages)
        converted_messages = build_anthropic_messages(msgs)
        client = AsyncAnthropicVertex(project_id=self._project_id, region=self._region)
        converted_tools = anthropic_tool_defs(tools) if tools else None
        try:
            return await count_anthropic_input_tokens(
                client,
                anthropic_module=anthropic,
                model=model,
                system=system,
                messages=cast(Any, converted_messages),
                tools=cast(Any, converted_tools),
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "token counting" in msg or "not supported" in msg:
                estimated = estimate_anthropic_input_tokens(
                    system=system,
                    messages=converted_messages,
                    tools=converted_tools,
                )
                _log.warning(
                    "Vertex Claude count_tokens unavailable, using estimate %s",
                    kv(provider="vertex_claude", model=model, estimated_tokens=estimated),
                    exc_info=True,
                )
                return estimated
            raise

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        hints: ProviderCallHints | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del thinking_budget
        import anthropic  # noqa: PLC0415
        from anthropic import AsyncAnthropicVertex  # noqa: PLC0415

        retention = hints.cache_retention if hints is not None else "short"
        system, msgs = split_leading_system(messages)
        client = AsyncAnthropicVertex(project_id=self._project_id, region=self._region)
        system_param, converted_messages, tools_param = prepare_anthropic_cached_payload(
            system=system,
            messages=msgs,
            tools=tools,
            cache_retention=retention,
            not_given=anthropic.NOT_GIVEN,
        )

        stream_kwargs: dict[str, Any] = {
            "model": model,
            "system": system_param,
            "messages": cast(Any, converted_messages),
            "tools": tools_param,
            "max_tokens": self._max_tokens,
        }
        if supports_param(model, "temperature"):
            stream_kwargs["temperature"] = self._temperature
        async for event in iter_anthropic_sdk_stream(
            client,
            stream_kwargs,
            provider="vertex_claude",
            error_message="Vertex Claude stream error: %s",
            n_messages=len(messages),
            n_tools=len(tools),
        ):
            yield event
