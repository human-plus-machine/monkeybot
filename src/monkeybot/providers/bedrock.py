"""Anthropic Claude on AWS Bedrock (``AsyncAnthropicBedrock``)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import (
    build_anthropic_messages,
    build_cached_system_blocks,
    estimate_anthropic_input_tokens,
    mark_last_tool_cached,
    safe_parse_tool_args,
)
from monkeybot.providers.sampling import resolve_model_sampling

_log = logging.getLogger(__name__)


class BedrockClaudeProvider:
    """Claude on AWS Bedrock via the Anthropic Bedrock client."""

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
        cache_enabled: bool = True,
    ) -> None:
        self._aws_region = (
            (aws_region or "").strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
            or "us-east-1"
        )
        self._cache_enabled = cache_enabled
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens

    def _convert_tools(self, tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    def _client(self) -> Any:
        from anthropic import AsyncAnthropicBedrock  # noqa: PLC0415

        return AsyncAnthropicBedrock(aws_region=self._aws_region)

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
        client = self._client()
        try:
            resp = await client.messages.count_tokens(
                model=model,
                system=system or anthropic.NOT_GIVEN,
                messages=converted_messages,
                tools=converted_tools,
            )
            return int(resp.input_tokens)
        except Exception as exc:
            msg = str(exc).lower()
            if "token counting" in msg or "not supported" in msg or "bedrock" in msg:
                _log.warning(
                    "Bedrock count_tokens unavailable, using estimate %s",
                    kv(provider="bedrock", model=model),
                    exc_info=True,
                )
                return estimate_anthropic_input_tokens(
                    system=system,
                    messages=converted_messages,
                    tools=self._convert_tools(tools) if tools else None,
                )
            raise

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del thinking_budget
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

        client = self._client()

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
                messages=converted_messages,
                tools=tools_param,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
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
                                args, parse_error = safe_parse_tool_args(
                                    _tool_input_buf,
                                    call_id=_tool_id,
                                    tool_name=_tool_name,
                                    provider="bedrock",
                                )
                                yield ToolCall(
                                    call_id=_tool_id,
                                    name=_tool_name,
                                    args=args,
                                    parse_error=parse_error,
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
        except Exception:
            _log.warning(
                "Bedrock Claude stream error %s",
                kv(provider="bedrock", model=model, n_messages=len(messages), n_tools=len(tools)),
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
        yield Done()
