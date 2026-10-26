"""Provider adapter contracts T1-PROV-01..10. No network."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from monkeybot.core.llm.provider import (
    Done,
    Message,
    ProviderCallHints,
    TextDelta,
    ThinkingDelta,
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
from monkeybot.providers import _openai_compat
from monkeybot.providers._bedrock_converse import _iter_converse_events, messages_to_converse
from monkeybot.providers._openai_compat import (
    ProviderRateLimitError,
    ProviderServerError,
    count_openai_compat_input_tokens,
    iter_openai_compat_stream,
    messages_to_openai,
    stream_chat_completions_with_tool_fallback,
    tiktoken_encoding_for_model,
)
from monkeybot.providers._utils import (
    build_anthropic_messages,
    estimate_anthropic_input_tokens,
    iter_anthropic_sdk_stream,
    reset_anthropic_token_estimate_correction,
    safe_parse_tool_args,
    split_leading_system,
)
from monkeybot.providers.bedrock import BedrockProvider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.fireworks import FireworksProvider
from monkeybot.providers.gemini import (
    GeminiProvider,
    _finalize_function_call_args,
    _merge_function_call_args,
    _messages_to_contents,
    _split_system_and_rest,
)
from monkeybot.providers.huggingface import HuggingFaceProvider
from monkeybot.providers.nvidia import NvidiaProvider
from monkeybot.providers.ollama import OllamaProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.openrouter import OpenRouterProvider
from monkeybot.providers.request_budget import (
    RequestByteBudgetError,
    trim_message_media_for_byte_budget,
)
from monkeybot.providers.vertex_claude import VertexClaudeProvider
from testkit.conversations import canonical_conversation, make_anthropic_stream_mock

_TABLE = Path(__file__).parent / "tables" / "provider_error_mapping.yaml"
_ERROR_ROWS = yaml.safe_load(_TABLE.read_text(encoding="utf-8"))["rows"]
_REQUIRED_ERROR_ROWS = {
    "rate_limit": {"raises": "ProviderRateLimitError", "attempts": 3, "provider": "openai"},
    "server_5xx": {"raises": "ProviderServerError", "attempts": 3, "provider": "openai"},
    "tool_unsupported": {"retry": "drop_tools", "attempts": 2, "provider": "huggingface"},
    "ollama_body_read_400": {"retry": "strip_media", "attempts": 2, "provider": "ollama"},
    "no_retry_after_first_event": {
        "raises": "ProviderRateLimitError",
        "attempts": 1,
        "provider": "openai",
    },
}
_STREAM_ORDER = ["text", "thinking", "tool", "usage", "done"]
_ANTHROPIC_CONTROL = {
    "none": None,
    "short": {"type": "ephemeral"},
    "long": {"type": "ephemeral", "ttl": "1h"},
}
_ZERO_USAGE = UsageEvent(
    input_tokens=0,
    output_tokens=0,
    cached_tokens=0,
    cache_read_tokens=0,
    cache_creation_tokens=0,
)
_ECHO = ToolDef(
    name="echo",
    description="echo",
    input_schema={"type": "object", "properties": {}},
)


@pytest.fixture(autouse=True)
def _reset_provider_memory() -> Any:
    from monkeybot.providers.rejected_sampling import clear_rejected_sampling

    clear_rejected_sampling()
    reset_anthropic_token_estimate_correction()
    yield
    clear_rejected_sampling()
    reset_anthropic_token_estimate_correction()


def _requests(messages: Sequence[Message]) -> list[ToolRequest]:
    return [
        block for message in messages for block in message.content if isinstance(block, ToolRequest)
    ]


def _thinking(messages: Sequence[Message]) -> Thinking:
    return next(
        block for message in messages for block in message.content if isinstance(block, Thinking)
    )


def _media(messages: Sequence[Message]) -> tuple[Image, File]:
    image: Image | None = None
    document: File | None = None
    for message in messages:
        for block in message.content:
            if not isinstance(block, ToolResponse):
                continue
            for item in block.result:
                if isinstance(item, Image):
                    image = item
                elif isinstance(item, File):
                    document = item
    assert image is not None and document is not None
    return image, document


def _kinds(events: Sequence[object]) -> list[str]:
    out: list[str] = []
    for event in events:
        if isinstance(event, TextDelta):
            kind = "text"
        elif isinstance(event, ThinkingDelta):
            kind = "thinking"
        elif isinstance(event, (ToolCall, ToolInputDelta)):
            kind = "tool"
        elif isinstance(event, UsageEvent):
            kind = "usage"
        elif isinstance(event, Done):
            kind = "done"
        else:
            kind = type(event).__name__
        if not out or out[-1] != kind:
            out.append(kind)
    return out


def _text_chunk(
    content: str | None = None,
    *,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(content=content, reasoning=reasoning, tool_calls=tool_calls),
            )
        ],
    )


def _usage_chunk(prompt: int, completion: int, *, cached: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
        choices=[],
    )


def _openai_client(chunks: list[Any]) -> Any:
    async def _create(**_kwargs: Any) -> AsyncIterator[Any]:
        async def _stream() -> AsyncIterator[Any]:
            for chunk in chunks:
                yield chunk

        return _stream()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))


class _RejectedError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status_code = 400


class _Enter:
    def __init__(self, exc: BaseException | None = None, events: list[Any] | None = None) -> None:
        self._exc = exc
        self._events = events or []

    async def __aenter__(self) -> AsyncIterator[Any]:
        if self._exc is not None and not self._events:
            raise self._exc

        async def _gen() -> AsyncIterator[Any]:
            for event in self._events:
                yield event
            if self._exc is not None:
                raise self._exc

        return _gen()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


def _cache_controls(value: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "cache_control" and isinstance(item, dict):
                found.append(item)
            else:
                found.extend(_cache_controls(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_cache_controls(item))
    return found


def _plain(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    return value


# --- T1-PROV-01 -----------------------------------------------------------


async def _assert_openai(messages: list[Message]) -> None:
    system, wired = await messages_to_openai(messages)
    thinking = _thinking(messages)
    image, document = _media(messages)
    assert system == "You are a careful assistant."
    assert [row["role"] for row in wired] == ["user", "assistant", "tool", "tool", "user"]
    calls = wired[1]["tool_calls"]
    assert [call["id"] for call in calls] == [block.id for block in _requests(messages)]
    assert wired[2]["tool_call_id"] == "c-echo"
    assert wired[3]["tool_call_id"] == "c-ls"
    echo = wired[2]["content"]
    assert "done" in echo
    assert "see the image below" in echo
    assert "pixels omitted for this provider" in echo
    assert document.data not in echo
    blob = json.dumps(wired)
    assert thinking.thinking not in blob
    assert thinking.signature not in blob
    promoted = wired[-1]["content"]
    assert any(
        part.get("type") == "image_url" and image.data in part["image_url"]["url"]
        for part in promoted
        if isinstance(part, dict)
    )


def _assert_anthropic(messages: list[Message]) -> None:
    system, rest = split_leading_system(messages)
    wired = build_anthropic_messages(rest)
    thinking = _thinking(messages)
    image, document = _media(messages)
    assert system == "You are a careful assistant."
    assert [row["role"] for row in wired] == ["user", "assistant", "user"]
    assistant = wired[1]["content"]
    assert [block["type"] for block in assistant] == ["thinking", "text", "tool_use", "tool_use"]
    assert assistant[0]["thinking"] == thinking.thinking
    assert assistant[0]["signature"] == thinking.signature
    assert [block["id"] for block in assistant if block["type"] == "tool_use"] == [
        block.id for block in _requests(messages)
    ]
    results = [block for block in wired[2]["content"] if block["type"] == "tool_result"]
    assert [block["tool_use_id"] for block in results] == [
        block.id for block in _requests(messages)
    ]
    body = results[0]["content"]
    assert {"type": "text", "text": "done"} in body
    assert {
        "type": "image",
        "source": {"type": "base64", "media_type": image.mime_type, "data": image.data},
    } in body
    assert {
        "type": "document",
        "source": {"type": "base64", "media_type": document.mime_type, "data": document.data},
    } in body


def _assert_gemini(messages: list[Message]) -> None:
    system, rest = _split_system_and_rest(messages)
    contents = _messages_to_contents(rest)
    thinking = _thinking(messages)
    image, document = _media(messages)
    assert system == "You are a careful assistant."
    assert [content.role for content in contents] == ["user", "model", "user"]
    model_parts = list(contents[1].parts)
    assert model_parts[0].thought is True
    assert model_parts[0].text == thinking.thinking
    assert model_parts[0].thought_signature == thinking.signature.encode()
    calls = [part.function_call for part in model_parts if part.function_call is not None]
    assert [call.id for call in calls] == [block.id for block in _requests(messages)]
    responses = [
        part.function_response for part in contents[2].parts if part.function_response is not None
    ]
    assert [response.id for response in responses] == [block.id for block in _requests(messages)]
    blobs = [
        part.inline_data
        for part in contents[2].parts
        if getattr(part, "inline_data", None) is not None
    ]
    assert [(blob.mime_type, bytes(blob.data)) for blob in blobs] == [
        (image.mime_type, base64.b64decode(image.data)),
        (document.mime_type, base64.b64decode(document.data)),
    ]


def _assert_converse(messages: list[Message]) -> None:
    system, wired = messages_to_converse(messages)
    thinking = _thinking(messages)
    image, document = _media(messages)
    assert system == "You are a careful assistant."
    assert [row["role"] for row in wired] == ["user", "assistant", "user"]
    assistant = wired[1]["content"]
    reasoning = assistant[0]["reasoningContent"]["reasoningText"]
    assert reasoning["text"] == thinking.thinking
    assert reasoning["signature"] == thinking.signature
    uses = [block["toolUse"] for block in assistant if "toolUse" in block]
    assert [use["toolUseId"] for use in uses] == [block.id for block in _requests(messages)]
    results = [block["toolResult"] for block in wired[2]["content"] if "toolResult" in block]
    assert [result["toolUseId"] for result in results] == [
        block.id for block in _requests(messages)
    ]
    body = results[0]["content"]
    assert {"text": "done"} in body
    assert {"image": {"format": "png", "source": {"bytes": base64.b64decode(image.data)}}} in body
    assert any(
        block.get("document", {}).get("format") == "pdf"
        and block["document"]["source"]["bytes"] == base64.b64decode(document.data)
        for block in body
    )


@pytest.mark.case("T1-PROV-01")
@pytest.mark.parametrize("adapter", ["openai", "anthropic", "gemini", "converse"])
async def test_converter_conformance(adapter: str) -> None:
    messages = canonical_conversation()
    if adapter == "openai":
        await _assert_openai(messages)
    elif adapter == "anthropic":
        _assert_anthropic(messages)
    elif adapter == "gemini":
        _assert_gemini(messages)
    else:
        _assert_converse(messages)


# --- T1-PROV-02 -----------------------------------------------------------


_FRAGMENTS = [
    pytest.param(['{"a": 1}'], id="valid-object"),
    pytest.param([""], id="empty"),
    pytest.param(['{"a":'], id="truncated"),
    pytest.param(["[1, 2]"], id="json-array"),
    pytest.param(['{"q": "hel', 'lo"}'], id="split-mid-string"),
]


async def _fragment_result(adapter: str, fragments: list[str]) -> Any:
    try:
        if adapter == "anthropic":
            return await _anthropic_fragments(fragments)
        if adapter == "converse":
            return await _converse_fragments(fragments)
        if adapter == "openai":
            return await _openai_fragments(fragments)
        return _gemini_fragments(fragments)
    except Exception as exc:
        return exc


async def _anthropic_fragments(fragments: list[str]) -> tuple[dict[str, Any], str | None]:
    events: list[Any] = [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", id="c1", name="echo"),
        )
    ]
    events.extend(
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json=fragment),
        )
        for fragment in fragments
    )
    events.append(SimpleNamespace(type="content_block_stop"))
    client = make_anthropic_stream_mock(events)
    found = [
        event
        for event in await _collect(
            iter_anthropic_sdk_stream(
                client,
                {"model": "claude-contract", "max_tokens": 16, "messages": []},
                provider="claude",
                error_message="unused %s",
            )
        )
        if isinstance(event, ToolCall)
    ]
    assert len(found) == 1
    return dict(found[0].args), found[0].parse_error


async def _converse_fragments(fragments: list[str]) -> tuple[dict[str, Any], str | None]:
    events: list[dict[str, Any]] = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "c1", "name": "echo"}},
            }
        }
    ]
    events.extend(
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"toolUse": {"input": fragment}},
            }
        }
        for fragment in fragments
    )
    events.append({"contentBlockStop": {"contentBlockIndex": 0}})
    found = [
        event
        for event in await _collect(_iter_converse_events(events, provider="bedrock"))
        if isinstance(event, ToolCall)
    ]
    assert len(found) == 1
    return dict(found[0].args), found[0].parse_error


async def _openai_fragments(fragments: list[str]) -> tuple[dict[str, Any], str | None]:
    chunks = []
    for index, fragment in enumerate(fragments):
        chunks.append(
            _text_chunk(
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="c1" if index == 0 else None,
                        function=SimpleNamespace(
                            name="echo" if index == 0 else None,
                            arguments=fragment,
                        ),
                    )
                ]
            )
        )
    found = [
        event
        for event in await _collect(
            iter_openai_compat_stream(
                _openai_client(chunks),
                {"model": "contract", "stream": True},
                provider="openai",
                n_tools=0,
            )
        )
        if isinstance(event, ToolCall)
    ]
    assert len(found) == 1
    return dict(found[0].args), found[0].parse_error


def _gemini_fragments(fragments: list[str]) -> tuple[dict[str, Any], str | None]:
    merged: dict[str, Any] = {}
    for fragment in fragments:
        merged = _merge_function_call_args(
            merged, SimpleNamespace(args=None, partial_args=fragment)
        )
    return _finalize_function_call_args(merged, call_id="c1", tool_name="echo")


@pytest.mark.case("T1-PROV-02")
@pytest.mark.parametrize("adapter", ["anthropic", "converse", "openai", "gemini"])
@pytest.mark.parametrize("fragments", _FRAGMENTS)
async def test_tool_arg_fragment_conformance(adapter: str, fragments: list[str]) -> None:
    expected = safe_parse_tool_args(
        "".join(fragments), call_id="c1", tool_name="echo", provider="contract"
    )
    got = await _fragment_result(adapter, fragments)
    assert got == expected


# --- T1-PROV-03 -----------------------------------------------------------


async def _order_events(adapter: str) -> list[Any]:
    if adapter == "anthropic":
        events = [
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text="order-text"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="order-think"),
            ),
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(type="tool_use", id="c1", name="echo"),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"a": 1}'),
            ),
            SimpleNamespace(type="content_block_stop"),
            SimpleNamespace(
                type="message_delta",
                delta=SimpleNamespace(stop_reason="end_turn"),
                usage=SimpleNamespace(
                    output_tokens=2,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            ),
        ]
        return await _collect(
            iter_anthropic_sdk_stream(
                make_anthropic_stream_mock(events),
                {"model": "claude-contract", "max_tokens": 16, "messages": []},
                provider="claude",
                error_message="unused %s",
            )
        )
    if adapter == "openai":
        chunks = [
            _text_chunk("order-text"),
            _text_chunk(reasoning="order-think"),
            _text_chunk(
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        id="c1",
                        function=SimpleNamespace(name="echo", arguments='{"a": 1}'),
                    )
                ]
            ),
            _usage_chunk(10, 2, cached=0),
        ]
        return await _collect(
            iter_openai_compat_stream(
                _openai_client(chunks),
                {"model": "contract", "stream": True},
                provider="openai",
                n_tools=0,
            )
        )
    if adapter == "converse":
        events = [
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "order-text"}}},
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 1,
                    "delta": {"reasoningContent": {"text": "order-think"}},
                }
            },
            {
                "contentBlockStart": {
                    "contentBlockIndex": 2,
                    "start": {"toolUse": {"toolUseId": "c1", "name": "echo"}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 2,
                    "delta": {"toolUse": {"input": '{"a": 1}'}},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 2}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 10,
                        "outputTokens": 2,
                        "cacheReadInputTokens": 0,
                        "cacheWriteInputTokens": 0,
                    }
                }
            },
            {"messageStop": {"stopReason": "end_turn"}},
        ]
        return await _collect(_iter_converse_events(events, provider="bedrock"))
    provider = GeminiProvider(
        api_key="test", temperature=0, max_tokens=16, thinking_budget=-1, reasoning_effort=""
    )
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=2,
            cached_content_token_count=0,
        ),
        candidates=[
            SimpleNamespace(
                finish_reason=None,
                grounding_metadata=None,
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text="order-text",
                            thought=False,
                            function_call=None,
                            thought_signature=None,
                        ),
                        SimpleNamespace(
                            text="order-think",
                            thought=True,
                            function_call=None,
                            thought_signature=None,
                        ),
                        SimpleNamespace(
                            text=None,
                            thought=False,
                            thought_signature=None,
                            function_call=SimpleNamespace(
                                name="echo", id="c1", args={"a": 1}, partial_args=None
                            ),
                        ),
                    ]
                ),
            )
        ],
    )

    async def _generate(**_kwargs: Any) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            yield response

        return _gen()

    provider._client = lambda _model: SimpleNamespace(  # type: ignore[method-assign]
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=_generate))
    )
    return await _collect(
        provider.stream(
            [Message.text("user", "hi")], [], model="gemini-2.5-flash", thinking_budget=-1
        )
    )


@pytest.mark.case("T1-PROV-03")
@pytest.mark.parametrize("adapter", ["anthropic", "openai", "gemini", "converse"])
async def test_stream_order_contract(adapter: str) -> None:
    events = await _order_events(adapter)
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["order-text"]
    assert [event.text for event in events if isinstance(event, ThinkingDelta)] == ["order-think"]
    assert _kinds(events) == _STREAM_ORDER


# --- T1-PROV-04 -----------------------------------------------------------


async def _usage_events(adapter: str, *, zero: bool, omit: bool = False) -> list[UsageEvent]:
    if adapter == "openai":
        chunk = _usage_chunk(0, 0, cached=0) if zero else _usage_chunk(100, 5, cached=40)
        events = await _collect(
            iter_openai_compat_stream(
                _openai_client([chunk]),
                {"model": "contract", "stream": True},
                n_tools=0,
            )
        )
    elif adapter == "anthropic":
        if zero:
            raw: list[Any] = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="x"),
                )
            ]
        else:
            raw = [
                SimpleNamespace(
                    type="message_start",
                    message=SimpleNamespace(
                        usage=SimpleNamespace(
                            input_tokens=30,
                            cache_read_input_tokens=11,
                            cache_creation_input_tokens=7,
                        )
                    ),
                ),
                SimpleNamespace(
                    type="message_delta",
                    delta=SimpleNamespace(stop_reason="end_turn"),
                    usage=SimpleNamespace(
                        output_tokens=4,
                        cache_read_input_tokens=0,
                        cache_creation_input_tokens=0,
                    ),
                ),
            ]
        events = await _collect(
            iter_anthropic_sdk_stream(
                make_anthropic_stream_mock(raw),
                {"model": "claude-contract", "max_tokens": 16, "messages": []},
                provider="claude",
                error_message="unused %s",
            )
        )
    elif adapter == "converse":
        if zero:
            raw_events: list[dict[str, Any]] = [{"contentBlockDelta": {"delta": {"text": "x"}}}]
        else:
            raw_events = [
                {
                    "metadata": {
                        "usage": {
                            "inputTokens": 30,
                            "outputTokens": 4,
                            "cacheReadInputTokens": 11,
                            "cacheWriteInputTokens": 7,
                        }
                    }
                }
            ]
        events = await _collect(_iter_converse_events(raw_events, provider="bedrock"))
    else:
        if omit:
            usage = None
        elif zero:
            usage = SimpleNamespace(
                prompt_token_count=0,
                candidates_token_count=0,
                cached_content_token_count=0,
            )
        else:
            usage = SimpleNamespace(
                prompt_token_count=30,
                candidates_token_count=4,
                cached_content_token_count=11,
            )
        response = SimpleNamespace(
            usage_metadata=usage,
            candidates=[
                SimpleNamespace(
                    finish_reason=None,
                    grounding_metadata=None,
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text="x",
                                thought=False,
                                function_call=None,
                                thought_signature=None,
                            )
                        ]
                    ),
                )
            ],
        )

        async def _generate(**_kwargs: Any) -> AsyncIterator[Any]:
            async def _gen() -> AsyncIterator[Any]:
                yield response

            return _gen()

        provider = GeminiProvider(
            api_key="test", temperature=0, max_tokens=16, thinking_budget=-1, reasoning_effort=""
        )
        provider._client = lambda _model: SimpleNamespace(  # type: ignore[method-assign]
            aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=_generate))
        )
        events = await _collect(
            provider.stream(
                [Message.text("user", "hi")], [], model="gemini-2.5-flash", thinking_budget=-1
            )
        )
    return [event for event in events if isinstance(event, UsageEvent)]


@pytest.mark.case("T1-PROV-04")
@pytest.mark.parametrize("adapter", ["openai", "anthropic", "gemini", "converse"])
async def test_usage_semantics(adapter: str) -> None:
    if adapter == "gemini":
        assert await _usage_events(adapter, zero=False, omit=True) == []
        assert await _usage_events(adapter, zero=True) == [_ZERO_USAGE]
        return
    assert await _usage_events(adapter, zero=True) == [_ZERO_USAGE]
    usage = (await _usage_events(adapter, zero=False))[0]
    if adapter == "openai":
        assert usage.input_tokens == 100 - 40
        assert usage.cached_tokens == 40
        assert usage.cache_read_tokens == 40
        assert usage.cache_creation_tokens == 0
        assert usage.output_tokens == 5
        return
    assert usage.cache_read_tokens == 11
    assert usage.cache_creation_tokens == 7
    assert usage.cached_tokens == usage.cache_read_tokens + usage.cache_creation_tokens
    assert usage.input_tokens == 30
    assert usage.output_tokens == 4


# --- T1-PROV-05 -----------------------------------------------------------


class _YieldThenRaise:
    def __init__(self, chunks: list[Any], exc: BaseException) -> None:
        self.chunks = chunks
        self.exc = exc


def _install_status_error(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    module = ModuleType("openai")

    class APIError(Exception):
        def __init__(self, message: str = "", *, body: object = None) -> None:
            super().__init__(message)
            self.message = message
            self.body = body

    class APIStatusError(APIError):
        def __init__(self, status_code: int, message: str = "", *, body: object = None) -> None:
            super().__init__(message, body=body)
            self.status_code = status_code

    class RateLimitError(APIStatusError):
        def __init__(self, message: str = "") -> None:
            super().__init__(429, message)

    module.APIError = APIError  # type: ignore[attr-defined]
    module.APIStatusError = APIStatusError  # type: ignore[attr-defined]
    module.RateLimitError = RateLimitError  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "openai", module)
    return APIStatusError


def _script_for(row: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    trigger = row["trigger"]
    if trigger == "rate_limit":
        return [Exception("too many requests")] * row["attempts"]
    if trigger == "server_5xx":
        status = _install_status_error(monkeypatch)
        return [status(500, "internal server error")] * row["attempts"]
    if trigger == "tool_unsupported":
        return [Exception("tool calling is unsupported"), []]
    if trigger == "ollama_body_read_400":
        return [Exception("failed to read request body"), []]
    if trigger == "rate_limit_after_yield":
        return [_YieldThenRaise([_text_chunk("partial")], Exception("too many requests"))]
    raise AssertionError(f"unknown trigger {trigger}")


@pytest.mark.case("T1-PROV-05")
@pytest.mark.parametrize("row", _ERROR_ROWS, ids=[row["id"] for row in _ERROR_ROWS])
async def test_error_mapping_table(row: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = {item["id"]: item for item in _ERROR_ROWS}
    for row_id, expect in _REQUIRED_ERROR_ROWS.items():
        for field, value in expect.items():
            assert loaded[row_id][field] == value

    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_openai_compat.asyncio, "sleep", _sleep)
    calls: list[dict[str, Any]] = []
    script = _script_for(row, monkeypatch)

    async def _create(**kwargs: Any) -> AsyncIterator[Any]:
        calls.append(kwargs)
        step = script[len(calls) - 1]
        if isinstance(step, BaseException):
            raise step

        async def _stream() -> AsyncIterator[Any]:
            chunks = step.chunks if isinstance(step, _YieldThenRaise) else step
            for chunk in chunks:
                yield chunk
            if isinstance(step, _YieldThenRaise):
                raise step.exc

        return _stream()

    monkeypatch.setattr(
        _openai_compat,
        "cached_async_openai",
        lambda *_args, **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ),
    )
    messages: list[Message]
    tools: list[ToolDef] = []
    max_bytes = None
    if row["trigger"] == "tool_unsupported":
        messages = [Message.text("user", "hi")]
        tools = [_ECHO]
    elif row["trigger"] == "ollama_body_read_400":
        messages = [
            Message(
                role="user",
                content=[Text(text="hi"), Image(mime_type="image/png", data="Q" * 4000)],
            )
        ]
        max_bytes = 8_000_000
    else:
        messages = [Message.text("user", "hi")]

    async def _run() -> None:
        async for _event in stream_chat_completions_with_tool_fallback(
            base_url="http://127.0.0.1:9/v1",
            api_key="test",
            provider=row["provider"],
            messages=messages,
            tools=tools,
            model="contract-model",
            temperature=0,
            max_tokens=16,
            max_request_bytes=max_bytes,
        ):
            pass

    raised = row.get("raises")
    if raised == "ProviderRateLimitError":
        with pytest.raises(ProviderRateLimitError):
            await _run()
    elif raised == "ProviderServerError":
        with pytest.raises(ProviderServerError):
            await _run()
    else:
        await _run()
    assert len(calls) == row["attempts"]
    if row.get("retry") == "drop_tools":
        assert "tools" in calls[0]
        assert "tools" not in calls[1]
    if row.get("retry") == "strip_media":
        assert "Q" * 40 in json.dumps(calls[0]["messages"])
        second = json.dumps(calls[1]["messages"])
        assert "Q" * 40 not in second
        assert "image omitted" in second


# --- T1-PROV-06 -----------------------------------------------------------


def _sampling_client(seen: list[dict[str, Any]], *, fail_before_content: bool) -> Any:
    blames = ("temperature: unsupported", "thinking: unsupported", "output_config: unsupported")

    def _stream(**kwargs: Any) -> _Enter:
        seen.append(dict(kwargs))
        if fail_before_content:
            if len(seen) <= len(blames):
                return _Enter(exc=_RejectedError(blames[len(seen) - 1]))
            return _Enter()
        return _Enter(
            exc=_RejectedError("temperature: unsupported"),
            events=[
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="hi"),
                )
            ],
        )

    return SimpleNamespace(messages=SimpleNamespace(stream=_stream))


_SAMPLING = {
    "model": "claude-contract",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
    "temperature": 0.2,
    "thinking": {"type": "enabled", "budget_tokens": 100},
    "output_config": {"effort": "low"},
}


@pytest.mark.case("T1-PROV-06")
async def test_sampling_strip_retry() -> None:
    seen: list[dict[str, Any]] = []
    await _collect(
        iter_anthropic_sdk_stream(
            _sampling_client(seen, fail_before_content=True),
            dict(_SAMPLING),
            provider="claude",
            error_message="unused %s",
        )
    )
    assert [sorted(set(_SAMPLING) & set(call)) for call in seen] == [
        ["max_tokens", "messages", "model", "output_config", "temperature", "thinking"],
        ["max_tokens", "messages", "model", "output_config", "thinking"],
        ["max_tokens", "messages", "model", "output_config"],
        ["max_tokens", "messages", "model"],
    ]
    assert "temperature" not in seen[-1]
    assert "thinking" not in seen[-1]
    assert "output_config" not in seen[-1]


@pytest.mark.case("T1-PROV-06")
async def test_sampling_no_retry_after_content() -> None:
    seen: list[dict[str, Any]] = []
    events: list[Any] = []
    with pytest.raises(_RejectedError):
        async for event in iter_anthropic_sdk_stream(
            _sampling_client(seen, fail_before_content=False),
            dict(_SAMPLING),
            provider="claude",
            error_message="unused %s",
        ):
            events.append(event)
    assert events == [TextDelta(text="hi")]
    assert len(seen) == 1
    assert "temperature" in seen[0]
    assert "thinking" in seen[0]
    assert "output_config" in seen[0]


# --- T1-PROV-07 -----------------------------------------------------------


async def _refusing_count(*_args: Any, **_kwargs: Any) -> int:
    raise RuntimeError("token counting is not supported")


@pytest.mark.case("T1-PROV-07")
@pytest.mark.parametrize(
    ("adapter", "model"),
    [
        ("vertex", "claude-sonnet-4-5"),
        ("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
    ],
)
async def test_token_count_api_fallback(
    adapter: str, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = [Message.text("user", "hello from the contract")]
    tools: list[ToolDef] = []
    if adapter == "vertex":
        provider: Any = VertexClaudeProvider(
            project_id="proj", region="global", temperature=0, max_tokens=16, reasoning_effort=""
        )
        provider._sdk_client = lambda: object()
        monkeypatch.setattr(
            "monkeybot.providers.vertex_claude.count_anthropic_input_tokens", _refusing_count
        )
    else:
        provider = BedrockProvider(temperature=0, max_tokens=16, reasoning_effort="")
        provider._client = lambda: object()
        monkeypatch.setattr(
            "monkeybot.providers.bedrock.count_anthropic_input_tokens", _refusing_count
        )
    system, rest = split_leading_system(messages)
    expected = estimate_anthropic_input_tokens(
        system=system,
        messages=build_anthropic_messages(rest),
        tools=None,
    )
    assert await provider.count_input_tokens(messages, tools, model=model) == expected


def _compat_provider(name: str) -> Any:
    kwargs = {"temperature": 0, "max_tokens": 16, "reasoning_effort": ""}
    if name == "openai":
        return OpenAIProvider(**kwargs)
    if name == "nvidia":
        return NvidiaProvider(**kwargs)
    if name == "openrouter":
        return OpenRouterProvider(**kwargs)
    if name == "fireworks":
        return FireworksProvider(**kwargs)
    if name == "huggingface":
        return HuggingFaceProvider(**kwargs)
    return OllamaProvider(mode="local", **kwargs)


@pytest.mark.case("T1-PROV-07")
@pytest.mark.parametrize(
    ("name", "env"),
    [
        ("openai", ("OPENAI_API_KEY", "sk-test")),
        ("nvidia", ("NVIDIA_API_KEY", "nvapi-test")),
        ("openrouter", ("OPENROUTER_API_KEY", "sk-or-test")),
        ("fireworks", ("FIREWORKS_API_KEY", "fw-test")),
        ("huggingface", ("HF_TOKEN", "hf-test")),
        ("ollama", None),
    ],
)
async def test_compat_token_count_uses_tiktoken(
    name: str, env: tuple[str, str] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env is not None:
        monkeypatch.setenv(*env)
    seen: list[str] = []
    real = tiktoken_encoding_for_model

    def _spy(model: str) -> Any:
        seen.append(model)
        return real(model)

    monkeypatch.setattr(_openai_compat, "tiktoken_encoding_for_model", _spy)
    monkeypatch.setattr("monkeybot.providers.openai.tiktoken_encoding_for_model", _spy)
    messages = [Message.text("user", "count these tokens")]
    model = "llama-contract"
    provider = _compat_provider(name)
    counted = await provider.count_input_tokens(messages, [], model=model)
    direct = await count_openai_compat_input_tokens(real(model), messages, [])
    assert seen == [model]
    assert real(model).name == "cl100k_base"
    assert counted == direct


# --- T1-PROV-08 -----------------------------------------------------------


async def _cache_observation(
    adapter: str, retention: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    hints = ProviderCallHints(session_id="sess-1", cache_retention=retention)  # type: ignore[arg-type]
    messages = [Message.text("system", "SYS"), Message.text("user", "hi")]
    if adapter in {"claude", "vertex", "bedrock"}:
        client = make_anthropic_stream_mock([])
        kwargs = {"temperature": 0, "max_tokens": 16, "reasoning_effort": ""}
        if adapter == "claude":
            monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
            monkeypatch.setattr(
                "monkeybot.providers.claude._anthropic_client", lambda _module: client
            )
            provider: Any = ClaudeProvider(**kwargs)
            model = "claude-sonnet-4-5"
        elif adapter == "vertex":
            provider = VertexClaudeProvider(project_id="proj", region="global", **kwargs)
            provider._sdk_client = lambda: client
            model = "claude-sonnet-4-5"
        else:
            provider = BedrockProvider(**kwargs)
            provider._client = lambda: client
            model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        async for _event in provider.stream(messages, [], model=model, hints=hints):
            pass
        sent = client.messages.stream.call_args.kwargs
        headers = sent.get("extra_headers") or {}
        return {
            "cache_controls": _cache_controls(sent),
            "prompt_cache_retention": sent.get("prompt_cache_retention"),
            "session_affinity": headers.get("x-session-affinity"),
        }

    captured: dict[str, Any] = {}

    async def _create(**kwargs: Any) -> AsyncIterator[Any]:
        captured["kwargs"] = kwargs

        async def _stream() -> AsyncIterator[Any]:
            if False:
                yield None

        return _stream()

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    if adapter == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(
            "monkeybot.providers.openai.cached_async_openai", lambda *_a, **_k: client
        )
        provider = OpenAIProvider(temperature=0, max_tokens=16, reasoning_effort="")
        async for _event in provider.stream(messages, [], model="gpt-4o-mini", hints=hints):
            pass
        sent = captured["kwargs"]
        headers = sent.get("extra_headers") or {}
        return {
            "cache_controls": _cache_controls(sent),
            "prompt_cache_retention": sent.get("prompt_cache_retention"),
            "session_affinity": headers.get("x-session-affinity"),
        }

    if adapter == "nvidia":
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        monkeypatch.setattr(_openai_compat, "cached_async_openai", lambda *_a, **_k: client)
        provider = NvidiaProvider(temperature=0, max_tokens=16, reasoning_effort="")
        async for _event in provider.stream(messages, [], model="meta/llama", hints=hints):
            pass
        sent = captured["kwargs"]
        headers = sent.get("extra_headers") or {}
        return {
            "cache_controls": _cache_controls(sent),
            "prompt_cache_retention": sent.get("prompt_cache_retention"),
            "session_affinity": headers.get("x-session-affinity"),
        }

    box: dict[str, Any] = {}

    async def _generate(**kwargs: Any) -> AsyncIterator[Any]:
        box["config"] = kwargs.get("config")

        async def _gen() -> AsyncIterator[Any]:
            yield SimpleNamespace(
                usage_metadata=None,
                candidates=[
                    SimpleNamespace(
                        finish_reason=None,
                        grounding_metadata=None,
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    text="x",
                                    thought=False,
                                    function_call=None,
                                    thought_signature=None,
                                )
                            ]
                        ),
                    )
                ],
            )

        return _gen()

    gemini = GeminiProvider(
        api_key="test", temperature=0, max_tokens=16, thinking_budget=-1, reasoning_effort=""
    )
    gemini._client = lambda _model: SimpleNamespace(  # type: ignore[method-assign]
        aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=_generate))
    )
    async for _event in gemini.stream(
        [Message.text("user", "hi")], [], model="gemini-2.5-flash", thinking_budget=-1
    ):
        pass
    config = _plain(box["config"])
    return {
        "cache_controls": _cache_controls(config),
        "prompt_cache_retention": None
        if not isinstance(config, dict)
        else config.get("prompt_cache_retention"),
        "session_affinity": None,
    }


@pytest.mark.case("T1-PROV-08")
@pytest.mark.parametrize("adapter", ["claude", "vertex", "bedrock", "openai", "gemini", "nvidia"])
@pytest.mark.parametrize("retention", ["none", "short", "long"])
async def test_cache_hints(adapter: str, retention: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = await _cache_observation(adapter, retention, monkeypatch)
    if adapter in {"claude", "vertex", "bedrock"}:
        expected = _ANTHROPIC_CONTROL[retention]
        if expected is None:
            assert observed["cache_controls"] == []
        else:
            assert observed["cache_controls"]
            assert all(control == expected for control in observed["cache_controls"])
    else:
        assert observed["cache_controls"] == []
    if adapter == "openai" and retention == "long":
        assert observed["prompt_cache_retention"] == "24h"
    else:
        assert observed["prompt_cache_retention"] is None
    if adapter in {"openai", "claude"} and retention != "none":
        assert observed["session_affinity"] == "sess-1"
    else:
        assert observed["session_affinity"] is None


# --- T1-PROV-09 -----------------------------------------------------------


@pytest.mark.case("T1-PROV-09")
@pytest.mark.parametrize(
    ("name", "env", "factory"),
    [
        ("nvidia", ("NVIDIA_API_KEY", "nvapi-test"), NvidiaProvider),
        ("openrouter", ("OPENROUTER_API_KEY", "sk-or-test"), OpenRouterProvider),
        ("fireworks", ("FIREWORKS_API_KEY", "fw-test"), FireworksProvider),
        ("huggingface", ("HF_TOKEN", "hf-test"), HuggingFaceProvider),
    ],
)
async def test_compat_providers_stream(
    name: str,
    env: tuple[str, str],
    factory: type[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del name
    monkeypatch.setenv(*env)
    fed = {"n": 0}

    async def _create(**_kwargs: Any) -> AsyncIterator[Any]:
        async def _stream() -> AsyncIterator[Any]:
            fed["n"] += 1
            yield _text_chunk("pong")
            yield _usage_chunk(4, 1, cached=1)

        return _stream()

    monkeypatch.setattr(
        _openai_compat,
        "cached_async_openai",
        lambda *_a, **_k: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ),
    )
    provider = factory(temperature=0, max_tokens=16, reasoning_effort="")
    events = await _collect(
        provider.stream([Message.text("user", "ping")], [], model="contract-model")
    )
    assert fed["n"] == 1
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["pong"]
    usage = [event for event in events if isinstance(event, UsageEvent)]
    assert usage == [
        UsageEvent(
            input_tokens=3,
            output_tokens=1,
            cached_tokens=1,
            cache_read_tokens=1,
            cache_creation_tokens=0,
        )
    ]
    assert isinstance(events[-1], Done)


# --- T1-PROV-10 -----------------------------------------------------------


@pytest.mark.case("T1-PROV-10")
async def test_request_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RequestByteBudgetError):
        trim_message_media_for_byte_budget(
            [Message.text("user", "text alone is already over the cap")],
            [],
            max_bytes=64,
            provider="openai",
        )

    order: list[str] = []
    real_trim = _openai_compat.trim_message_media_for_byte_budget
    real_extract = _openai_compat._extract_pdf_text

    def _spy_trim(*args: Any, **kwargs: Any) -> list[Message]:
        trimmed = real_trim(*args, **kwargs)
        order.append("trim")
        order.append(
            "trimmed-image"
            if any(isinstance(block, Image) for message in trimmed for block in message.content)
            else "trimmed-text"
        )
        return trimmed

    async def _spy_extract(block: File, hints: ProviderCallHints | None = None) -> str | None:
        order.append("extract")
        return await real_extract(block, hints)

    monkeypatch.setattr(_openai_compat, "trim_message_media_for_byte_budget", _spy_trim)
    monkeypatch.setattr(_openai_compat, "_extract_pdf_text", _spy_extract)

    async def _create(**kwargs: Any) -> AsyncIterator[Any]:
        order.append(json.dumps(kwargs["messages"]))

        async def _stream() -> AsyncIterator[Any]:
            if False:
                yield None

        return _stream()

    monkeypatch.setattr(
        _openai_compat,
        "cached_async_openai",
        lambda *_a, **_k: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        ),
    )
    payload = "Q" * 200_000
    pdf = File(mime_type="application/pdf", data="JVBERi0xLjQKbm90IGEgcmVhbCBwZGY=")
    async for _event in stream_chat_completions_with_tool_fallback(
        base_url="http://127.0.0.1:9/v1",
        api_key="test",
        provider="openai",
        messages=[
            Message(
                role="user",
                content=[
                    Text(text="see"),
                    Image(mime_type="image/png", data=payload),
                    pdf,
                ],
            )
        ],
        tools=[],
        model="contract-model",
        temperature=0,
        max_tokens=16,
        max_request_bytes=100_000,
    ):
        pass
    assert order[0] == "trim"
    assert "trimmed-text" in order
    assert order.index("trim") < order.index("extract")
    body = order[-1]
    assert payload[:80] not in body
    assert "pixels omitted for this provider" in body or "File attachment" in body
