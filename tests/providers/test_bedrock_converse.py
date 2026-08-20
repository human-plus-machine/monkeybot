"""Bedrock Converse path for non-Claude models (Grok, Nova, Llama)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from monkeybot.core.llm.provider import (
    Done,
    Message,
    TextDelta,
    ToolCall,
    ToolInputDelta,
    UsageEvent,
)
from monkeybot.core.types.content_blocks import (
    File,
    Image,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._bedrock_converse import (
    bedrock_vendor,
    converse_request_kwargs,
    converse_tools,
    estimate_converse_input_tokens,
    iter_converse_stream,
    messages_to_converse,
    uses_anthropic_bedrock,
)
from monkeybot.providers.bedrock import BedrockClaudeProvider, BedrockProvider
from monkeybot.providers.model_capabilities import supports_param
from tests.providers.conftest import make_anthropic_stream_mock

_ECHO = ToolDef(
    name="echo",
    description="echo a value",
    input_schema={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
)

_GROK = "us.xai.grok-4.6"
_NOVA = "us.amazon.nova-pro-v1:0"
_CLAUDE = "us.anthropic.claude-sonnet-4-6"


@pytest.mark.parametrize(
    ("model_id", "vendor"),
    [
        ("us.anthropic.claude-sonnet-4-6", "anthropic"),
        ("anthropic.claude-3-5-sonnet-20241022-v1:0", "anthropic"),
        ("claude-3-5-sonnet-20241022", "anthropic"),
        ("bedrock/us.anthropic.claude-sonnet-4-6", "anthropic"),
        ("anthropic/claude-sonnet-4-6", "anthropic"),
        ("us.xai.grok-4.6", "xai"),
        ("xai.grok-4.6", "xai"),
        ("xai/grok-4.6", "xai"),
        ("bedrock/us.xai.grok-4.6", "xai"),
        ("us.amazon.nova-pro-v1:0", "amazon"),
        ("amazon.nova-lite-v1:0", "amazon"),
        ("us.meta.llama3-3-70b-instruct-v1:0", "meta"),
        ("meta.llama3-70b-instruct-v1:0", "meta"),
    ],
)
def test_vendor_parse(model_id: str, vendor: str) -> None:
    assert bedrock_vendor(model_id) == vendor
    assert uses_anthropic_bedrock(model_id) is (vendor == "anthropic")


def test_toolspec_shape_not_openai_or_anthropic() -> None:
    config = converse_tools([_ECHO])
    assert config is not None
    spec = config["tools"][0]["toolSpec"]
    assert spec["name"] == "echo"
    assert spec["description"] == "echo a value"
    assert spec["inputSchema"] == {"json": _ECHO.input_schema}
    dumped = str(config)
    assert "input_schema" not in dumped
    assert '"type": "function"' not in dumped
    assert "function" not in spec


def test_grok_kwargs_omit_temperature_nova_keeps_it() -> None:
    messages = [Message.text("user", "hi")]
    grok = converse_request_kwargs(
        model=_GROK,
        messages=messages,
        tools=[],
        max_tokens=2048,
        temperature=0.2,
    )
    nova = converse_request_kwargs(
        model=_NOVA,
        messages=messages,
        tools=[],
        max_tokens=2048,
        temperature=0.2,
    )
    assert grok["inferenceConfig"] == {"maxTokens": 2048}
    assert "temperature" not in grok["inferenceConfig"]
    assert supports_param(_GROK, "temperature") is False
    assert nova["inferenceConfig"]["maxTokens"] == 2048
    assert nova["inferenceConfig"]["temperature"] == 0.2
    assert supports_param(_NOVA, "temperature") is True


def test_tool_round_trip_tooluse_and_toolresult() -> None:
    messages = [
        Message.text("user", "hi"),
        Message(
            role="assistant",
            content=[
                Text(text="ok"),
                ToolRequest(id="c1", name="echo", args={"x": 1}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(id="c1", tool_name="echo", result=[Text(text="done")]),
            ],
        ),
    ]
    system, converse_msgs = messages_to_converse(messages)
    assert system == ""
    assert converse_msgs[0] == {"role": "user", "content": [{"text": "hi"}]}
    assert converse_msgs[1]["role"] == "assistant"
    assert converse_msgs[1]["content"][0] == {"text": "ok"}
    assert converse_msgs[1]["content"][1] == {
        "toolUse": {"toolUseId": "c1", "name": "echo", "input": {"x": 1}},
    }
    assert converse_msgs[2] == {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "c1",
                    "content": [{"text": "done"}],
                }
            }
        ],
    }


def test_consecutive_user_messages_merge() -> None:
    messages = [
        Message.text("user", "first"),
        Message.text("user", "second"),
        Message.text("assistant", "reply"),
    ]
    _, converse_msgs = messages_to_converse(messages)
    assert converse_msgs == [
        {"role": "user", "content": [{"text": "first"}, {"text": "second"}]},
        {"role": "assistant", "content": [{"text": "reply"}]},
    ]


def test_thinking_mapped_to_reasoning_content() -> None:
    messages = [
        Message.text("user", "hi"),
        Message(
            role="assistant",
            content=[Thinking(thinking="secret", signature="sig"), Text(text="visible")],
        ),
    ]
    _, converse_msgs = messages_to_converse(messages)
    assert converse_msgs[1]["content"][0] == {
        "reasoningContent": {"reasoningText": {"text": "secret", "signature": "sig"}},
    }
    assert converse_msgs[1]["content"][1] == {"text": "visible"}


def test_assistant_first_history_prepends_user() -> None:
    _, converse_msgs = messages_to_converse([Message.text("assistant", "prior")])
    assert converse_msgs[0] == {"role": "user", "content": [{"text": "(continued)"}]}
    assert converse_msgs[1] == {"role": "assistant", "content": [{"text": "prior"}]}


def _converse_events() -> list[dict[str, object]]:
    return [
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "t1", "name": "echo"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": '{"x":'}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": "1}"}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 9, "outputTokens": 4}}},
    ]


@pytest.mark.asyncio
async def test_stream_emits_toolcall_from_converse_events() -> None:
    client = MagicMock()
    client.converse_stream.return_value = {"stream": iter(_converse_events())}
    events = [
        e
        async for e in iter_converse_stream(
            client,
            {"modelId": _GROK, "messages": [], "inferenceConfig": {"maxTokens": 16}},
            provider="bedrock",
            error_message="err %s",
        )
    ]
    deltas = [e for e in events if isinstance(e, ToolInputDelta)]
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert [d.delta for d in deltas] == ['{"x":', "1}"]
    assert len(calls) == 1
    assert calls[0].call_id == "t1"
    assert calls[0].name == "echo"
    assert calls[0].args == {"x": 1}
    assert isinstance(events[-2], UsageEvent)
    assert events[-2].input_tokens == 9
    assert isinstance(events[-1], Done)


def _text_stream() -> list[dict[str, object]]:
    return [
        {"contentBlockDelta": {"delta": {"text": "ok"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 2, "outputTokens": 1}}},
    ]


@pytest.mark.asyncio
async def test_grok_stream_calls_converse_not_anthropic() -> None:
    provider = BedrockClaudeProvider(temperature=0.2, max_tokens=2048, aws_region="us-east-1")
    runtime = MagicMock()
    runtime.converse_stream.return_value = {"stream": iter(_text_stream())}
    provider._runtime = runtime
    anthropic = MagicMock()
    provider._client = lambda: anthropic  # type: ignore[method-assign]

    events = [e async for e in provider.stream([Message.text("user", "hi")], [], model=_GROK)]

    anthropic.messages.stream.assert_not_called()
    runtime.converse_stream.assert_called_once()
    kwargs = runtime.converse_stream.call_args.kwargs
    assert kwargs["modelId"] == _GROK
    assert "temperature" not in kwargs["inferenceConfig"]
    assert kwargs["inferenceConfig"]["maxTokens"] == 2048
    assert any(isinstance(e, TextDelta) and e.text == "ok" for e in events)


@pytest.mark.asyncio
async def test_claude_stream_still_uses_anthropic_mock() -> None:
    provider = BedrockClaudeProvider(temperature=0.2, max_tokens=8192, aws_region="us-east-1")
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
        ),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=1)),
    ]
    client = make_anthropic_stream_mock(events)
    provider._client = lambda: client  # type: ignore[method-assign]
    runtime = MagicMock()
    provider._runtime = runtime

    async for _ in provider.stream([Message.text("user", "hi")], [], model=_CLAUDE):
        pass

    runtime.converse_stream.assert_not_called()
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_retry_strips_temperature_after_validation_exception() -> None:
    calls: list[dict[str, object]] = []

    def converse_stream(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        infer = kwargs["inferenceConfig"]  # type: ignore[index]
        if isinstance(infer, dict) and "temperature" in infer:
            raise Exception(
                "ValidationException: This model doesn't support the temperature field."
            )
        return {"stream": iter(_text_stream())}

    client = MagicMock()
    client.converse_stream.side_effect = converse_stream
    events = [
        e
        async for e in iter_converse_stream(
            client,
            {
                "modelId": _NOVA,
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
                "inferenceConfig": {"maxTokens": 32, "temperature": 0.2},
            },
            provider="bedrock",
            error_message="err %s",
        )
    ]
    assert len(calls) == 2
    assert calls[0]["inferenceConfig"] == {"maxTokens": 32, "temperature": 0.2}
    assert calls[1]["inferenceConfig"] == {"maxTokens": 32}
    assert any(isinstance(e, TextDelta) and e.text == "ok" for e in events)


@pytest.mark.asyncio
async def test_unexpected_stream_event_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    client = MagicMock()
    client.converse_stream.return_value = {"stream": iter(["not-a-dict", *_text_stream()])}
    with caplog.at_level("WARNING", logger="monkeybot.providers._bedrock_converse"):
        events = [
            e
            async for e in iter_converse_stream(
                client,
                {"modelId": _GROK, "messages": [], "inferenceConfig": {"maxTokens": 8}},
                provider="bedrock",
                error_message="err %s",
            )
        ]
    assert any("unexpected converse stream event" in rec.message for rec in caplog.records)
    assert any(isinstance(e, TextDelta) and e.text == "ok" for e in events)


def test_estimate_with_image_does_not_raise() -> None:
    n = estimate_converse_input_tokens(
        [
            Message(
                role="user",
                content=[
                    Text(text="see"),
                    Image(mime_type="image/png", data="QQ=="),
                    File(mime_type="application/pdf", data="QQ=="),
                ],
            )
        ],
        [],
    )
    assert n >= 1


@pytest.mark.asyncio
async def test_converse_count_skips_count_tokens_api() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1")
    runtime = MagicMock()
    provider._runtime = runtime
    n = await provider.count_input_tokens([Message.text("user", "hi")], [], model=_GROK)
    assert n >= 1
    runtime.count_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_client_is_cached() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1")
    runtime = MagicMock()
    builds: list[int] = []

    def _build() -> MagicMock:
        builds.append(1)
        return runtime

    provider._new_runtime_client = _build  # type: ignore[method-assign]
    assert await provider._runtime_client() is runtime
    assert await provider._runtime_client() is runtime
    assert builds == [1]


@pytest.mark.asyncio
async def test_empty_tool_use_id_logs_and_emits_toolcall(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream_events: list[dict[str, object]] = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "", "name": "echo"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": "{}"}},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    client = MagicMock()
    client.converse_stream.return_value = {"stream": iter(stream_events)}
    with caplog.at_level("WARNING", logger="monkeybot.providers._bedrock_converse"):
        events = [
            e
            async for e in iter_converse_stream(
                client,
                {"modelId": _GROK, "messages": [], "inferenceConfig": {"maxTokens": 8}},
                provider="bedrock",
                error_message="err %s",
            )
        ]
    assert any("missing toolUseId" in rec.message for rec in caplog.records)
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(calls) == 1
    assert calls[0].name == "echo"


@pytest.mark.asyncio
async def test_converse_stream_is_closed() -> None:
    class _Stream:
        def __init__(self) -> None:
            self.closed = False
            self._it = iter(_text_stream())

        def __iter__(self) -> _Stream:
            return self

        def __next__(self) -> dict[str, object]:
            return next(self._it)

        def close(self) -> None:
            self.closed = True

    stream = _Stream()
    client = MagicMock()
    client.converse_stream.return_value = {"stream": stream}
    async for _ in iter_converse_stream(
        client,
        {"modelId": _GROK, "messages": [], "inferenceConfig": {"maxTokens": 8}},
        provider="bedrock",
        error_message="err %s",
    ):
        pass
    assert stream.closed


def test_bedrock_provider_alias() -> None:
    assert BedrockProvider is BedrockClaudeProvider
