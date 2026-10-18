"""AWS Bedrock provider (Claude via Anthropic SDK; other models via Converse)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import Message, ProviderCallHints, ProviderEvent
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._bedrock_converse import (
    converse_request_kwargs,
    estimate_converse_input_tokens,
    iter_converse_stream,
    uses_anthropic_bedrock,
)
from monkeybot.providers._utils import (
    anthropic_sampling_kwargs,
    anthropic_tool_defs,
    build_anthropic_messages,
    count_anthropic_input_tokens,
    estimate_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    prepare_anthropic_cached_payload,
    split_leading_system,
    suppress_thinking_for_auxiliary_call,
)
from monkeybot.providers.request_budget import trim_message_media_for_byte_budget
from monkeybot.providers.sampling import resolve_model_sampling

_log = logging.getLogger(__name__)
_CONVERSE_EFFORT_WARNED: set[str] = set()


class BedrockProvider:
    """Bedrock models via Anthropic (Claude) or Converse (Grok, Nova, Llama, …)."""

    @property
    def name(self) -> str:
        return "bedrock"

    @property
    def supports_streaming(self) -> bool:
        return True

    def __init__(
        self,
        *,
        aws_region: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._aws_region = (
            (aws_region or "").strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
            or "us-east-1"
        )
        sampling = resolve_model_sampling(
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens
        self._reasoning_effort = sampling.reasoning_effort
        self._runtime: Any = None
        self._runtime_lock = asyncio.Lock()

    def _client(self) -> Any:
        from anthropic import AsyncAnthropicBedrock  # noqa: PLC0415

        return AsyncAnthropicBedrock(aws_region=self._aws_region)

    def _new_runtime_client(self) -> Any:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        return boto3.client(
            "bedrock-runtime",
            region_name=self._aws_region,
            config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
        )

    async def _runtime_client(self) -> Any:
        if self._runtime is not None:
            return self._runtime
        async with self._runtime_lock:
            if self._runtime is None:
                self._runtime = await asyncio.to_thread(self._new_runtime_client)
            return self._runtime

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
        messages = trim_message_media_for_byte_budget(
            messages,
            tools,
            provider=self.name,
            raise_if_oversized=False,
        )
        if not uses_anthropic_bedrock(model):
            return estimate_converse_input_tokens(messages, tools)
        import anthropic  # noqa: PLC0415

        system, msgs = split_leading_system(messages)
        converted_messages = build_anthropic_messages(msgs)
        converted_tools = anthropic_tool_defs(tools) if tools else None
        client = self._client()
        try:
            return await count_anthropic_input_tokens(
                client,
                anthropic_module=anthropic,
                model=model,
                system=system,
                messages=converted_messages,
                tools=converted_tools,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "token counting" in msg or "not supported" in msg or "bedrock" in msg:
                estimated = estimate_anthropic_input_tokens(
                    system=system,
                    messages=converted_messages,
                    tools=converted_tools,
                )
                _log.warning(
                    "Bedrock count_tokens unavailable, using estimate %s",
                    kv(provider="bedrock", model=model, estimated_tokens=estimated),
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
        # thinking_budget is not forwarded: Converse has no budget field, and the
        # Claude path sends reasoning_effort instead of a manual budget.
        del thinking_budget
        messages = trim_message_media_for_byte_budget(messages, tools, provider=self.name)
        if not uses_anthropic_bedrock(model):
            if self._reasoning_effort and model not in _CONVERSE_EFFORT_WARNED:
                _CONVERSE_EFFORT_WARNED.add(model)
                _log.warning(
                    "reasoning_effort is ignored on the Bedrock Converse path %s",
                    kv(
                        provider="bedrock",
                        model=model,
                        reasoning_effort=self._reasoning_effort,
                    ),
                )
            _log.info(
                "Bedrock dispatch %s",
                kv(provider="bedrock", model=model, path="converse"),
            )
            kwargs = converse_request_kwargs(
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
            client = await self._runtime_client()
            async for event in iter_converse_stream(
                client,
                kwargs,
                provider="bedrock",
                error_message="Bedrock Converse stream error: %s",
                n_messages=len(messages),
                n_tools=len(tools),
            ):
                yield event
            return

        _log.info(
            "Bedrock dispatch %s",
            kv(provider="bedrock", model=model, path="anthropic"),
        )
        import anthropic  # noqa: PLC0415

        retention = hints.cache_retention if hints is not None else "short"
        system, msgs = split_leading_system(messages)
        system_param, converted_messages, tools_param = prepare_anthropic_cached_payload(
            system=system,
            messages=msgs,
            tools=tools,
            cache_retention=retention,
            not_given=anthropic.NOT_GIVEN,
        )

        client = self._client()
        stream_kwargs: dict[str, Any] = {
            "model": model,
            "system": system_param,
            "messages": converted_messages,
            "tools": tools_param,
            "max_tokens": self._max_tokens,
            **anthropic_sampling_kwargs(
                model=model,
                temperature=self._temperature,
                thinking_budget=None,
                reasoning_effort=(
                    None
                    if suppress_thinking_for_auxiliary_call(messages, tools)
                    else self._reasoning_effort
                ),
            ),
        }
        async for event in iter_anthropic_sdk_stream(
            client,
            stream_kwargs,
            provider="bedrock",
            error_message="Bedrock Claude stream error: %s",
            n_messages=len(messages),
            n_tools=len(tools),
        ):
            yield event


__all__ = ["BedrockProvider"]
