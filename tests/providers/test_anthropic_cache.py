"""Tests for Anthropic cache_control markers and cache usage field mapping."""

from __future__ import annotations

import copy
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monkeybot.core.llm.provider import Message, UsageEvent
from monkeybot.providers._utils import (
    build_anthropic_messages,
    build_cached_system_blocks,
    mark_last_tool_cached,
)
from monkeybot.providers.bedrock import BedrockClaudeProvider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider
from tests.providers.conftest import (
    CANONICAL_TOOL_DEFS,
    make_anthropic_stream_mock,
    typed_messages_four_turn,
)

SYSTEM_TEXT = "SYS_PROMPT"
_TWO_TOOLS = list(CANONICAL_TOOL_DEFS)
_ONE_TOOL = [_TWO_TOOLS[0]]


def _messages_with_system() -> list[Message]:
    return [Message.text("system", SYSTEM_TEXT), Message.text("user", "hi")]


def _messages_no_system() -> list[Message]:
    return [Message.text("user", "hi")]


def _minimal_stream_events() -> list[object]:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(input_tokens=1)),
        ),
        SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=1)),
    ]


def _cache_usage_stream_events(
    *,
    input_tokens: int = 50,
    cache_read: int | None = 100,
    cache_creation: int | None = 20,
    delta_cache_read: int | None = None,
) -> list[object]:
    usage_kwargs: dict[str, int] = {"input_tokens": input_tokens}
    if cache_read is not None:
        usage_kwargs["cache_read_input_tokens"] = cache_read
    if cache_creation is not None:
        usage_kwargs["cache_creation_input_tokens"] = cache_creation
    events: list[object] = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=SimpleNamespace(**usage_kwargs)),
        ),
    ]
    delta_usage: dict[str, int] = {"output_tokens": 7}
    if delta_cache_read is not None:
        delta_usage["cache_read_input_tokens"] = delta_cache_read
    events.append(SimpleNamespace(type="message_delta", usage=SimpleNamespace(**delta_usage)))
    return events


async def _usage_from_stream(
    provider: ClaudeProvider | VertexClaudeProvider | BedrockClaudeProvider,
    messages: list[Message],
    tools: list[Any],
    *,
    model: str = "claude-3-5-sonnet-20241022",
) -> UsageEvent | None:
    usage: UsageEvent | None = None
    async for event in provider.stream(messages, tools, model=model):
        if isinstance(event, UsageEvent):
            usage = event
    return usage


def _expected_cached_system() -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": SYSTEM_TEXT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _assert_no_cache_control(value: object) -> None:
    assert "cache_control" not in str(value)


# --- Task 1: _utils helpers ---


def test_build_cached_system_blocks_shape() -> None:
    assert build_cached_system_blocks("SYS") == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]


def test_build_cached_system_blocks_splits_volatile_tail() -> None:
    system = "STABLE\n\n## Memory index\n- note"
    blocks = build_cached_system_blocks(system)
    assert blocks == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\n## Memory index\n- note"},
    ]


def test_mark_last_tool_cached_marks_only_last() -> None:
    tools = [
        {"name": "a", "description": "d", "input_schema": {}},
        {"name": "b", "description": "d", "input_schema": {}},
    ]
    marked = mark_last_tool_cached(tools)
    assert "cache_control" not in marked[0]
    assert marked[1]["cache_control"] == {"type": "ephemeral"}


def test_mark_last_tool_cached_does_not_mutate_input() -> None:
    tools = [{"name": "a", "description": "d", "input_schema": {}}]
    original = copy.deepcopy(tools)
    list_id = id(tools)
    dict_id = id(tools[0])
    marked = mark_last_tool_cached(tools)
    assert id(tools) == list_id
    assert id(tools[0]) == dict_id
    assert tools == original
    assert "cache_control" not in tools[0]
    assert marked[0]["cache_control"] == {"type": "ephemeral"}


def test_mark_last_tool_cached_empty_returns_unchanged() -> None:
    assert mark_last_tool_cached([]) == []


# --- Task 2: ClaudeProvider ---


@pytest.mark.asyncio
async def test_claude_enabled_system_is_cached_block_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    assert captured_system == _expected_cached_system()


@pytest.mark.asyncio
async def test_claude_enabled_last_tool_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_disabled_system_is_plain_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=False)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert captured_system == SYSTEM_TEXT
    _assert_no_cache_control(captured_system)
    _assert_no_cache_control(captured_tools)


@pytest.mark.asyncio
async def test_claude_disabled_byte_identical_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=False)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), [])

    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["system"] == SYSTEM_TEXT
    assert kwargs["tools"] is anthropic.NOT_GIVEN


@pytest.mark.asyncio
async def test_claude_enabled_empty_system_uses_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _usage_from_stream(provider, _messages_no_system(), _ONE_TOOL)

    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["system"] is anthropic.NOT_GIVEN
    assert kwargs["tools"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_usage_maps_cache_read_and_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    events = _cache_usage_stream_events()
    client = make_anthropic_stream_mock(events)

    with patch("anthropic.AsyncAnthropic", return_value=client):
        usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 100
    assert usage.cache_creation_tokens == 20
    assert usage.cached_tokens == 120


@pytest.mark.asyncio
async def test_claude_usage_missing_cache_fields_reads_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    events = _cache_usage_stream_events(cache_read=None, cache_creation=None)
    client = make_anthropic_stream_mock(events)

    with patch("anthropic.AsyncAnthropic", return_value=client):
        usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cached_tokens == 0


@pytest.mark.asyncio
async def test_claude_usage_cache_fields_on_message_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    events = _cache_usage_stream_events(
        cache_read=None,
        cache_creation=None,
        delta_cache_read=30,
    )
    client = make_anthropic_stream_mock(events)

    with patch("anthropic.AsyncAnthropic", return_value=client):
        usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.cache_read_tokens == 30
    assert usage.cached_tokens == 30


@pytest.mark.asyncio
async def test_claude_count_input_tokens_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider(cache_enabled=True)
    mock_client = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(
        return_value=SimpleNamespace(input_tokens=42)
    )

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await provider.count_input_tokens(
            _messages_with_system(),
            _TWO_TOOLS,
            model="claude-3-5-sonnet-20241022",
        )

    kwargs = mock_client.messages.count_tokens.await_args.kwargs
    assert kwargs["system"] == SYSTEM_TEXT
    assert "cache_control" not in kwargs["tools"][0]
    assert "cache_control" not in kwargs["tools"][1]


@pytest.mark.asyncio
async def test_vertex_count_input_tokens_falls_back_when_unsupported() -> None:
    provider = VertexClaudeProvider(project_id="p", region="us-east5", cache_enabled=True)
    mock_client = MagicMock()
    mock_client.messages.count_tokens = AsyncMock(
        side_effect=Exception(
            "Error code: 400 - claude-haiku-4-5 is not supported for token counting"
        )
    )

    with patch("anthropic.AsyncAnthropicVertex", return_value=mock_client):
        n = await provider.count_input_tokens(
            _messages_with_system(),
            _TWO_TOOLS,
            model="claude-haiku-4-5",
        )

    assert n > 0


# --- Task 3: VertexClaudeProvider ---


@pytest.mark.asyncio
async def test_vertex_enabled_system_is_cached_block_list() -> None:
    provider = VertexClaudeProvider(project_id="p", cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    assert captured_system == _expected_cached_system()


@pytest.mark.asyncio
async def test_vertex_enabled_last_tool_marked() -> None:
    provider = VertexClaudeProvider(project_id="p", cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_vertex_disabled_byte_identical() -> None:
    provider = VertexClaudeProvider(project_id="p", cache_enabled=False)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert captured_system == SYSTEM_TEXT
    _assert_no_cache_control(captured_system)
    _assert_no_cache_control(captured_tools)


@pytest.mark.asyncio
async def test_vertex_usage_maps_cache_read_and_creation() -> None:
    provider = VertexClaudeProvider(project_id="p", cache_enabled=True)
    client = make_anthropic_stream_mock(_cache_usage_stream_events())

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 100
    assert usage.cache_creation_tokens == 20
    assert usage.cached_tokens == 120


@pytest.mark.asyncio
async def test_vertex_usage_missing_cache_fields_reads_zero() -> None:
    provider = VertexClaudeProvider(project_id="p", cache_enabled=True)
    client = make_anthropic_stream_mock(
        _cache_usage_stream_events(cache_read=None, cache_creation=None)
    )

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cached_tokens == 0


# --- Task 4: BedrockClaudeProvider ---


@pytest.mark.asyncio
async def test_bedrock_enabled_system_is_cached_block_list() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1", cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())
    provider._client = lambda: client  # type: ignore[method-assign]

    await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    assert captured_system == _expected_cached_system()


@pytest.mark.asyncio
async def test_bedrock_enabled_last_tool_marked() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1", cache_enabled=True)
    client = make_anthropic_stream_mock(_minimal_stream_events())
    provider._client = lambda: client  # type: ignore[method-assign]

    await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_bedrock_disabled_byte_identical() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1", cache_enabled=False)
    client = make_anthropic_stream_mock(_minimal_stream_events())
    provider._client = lambda: client  # type: ignore[method-assign]

    await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert captured_system == SYSTEM_TEXT
    _assert_no_cache_control(captured_system)
    _assert_no_cache_control(captured_tools)


@pytest.mark.asyncio
async def test_bedrock_usage_maps_cache_read_and_creation() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1", cache_enabled=True)
    client = make_anthropic_stream_mock(_cache_usage_stream_events())
    provider._client = lambda: client  # type: ignore[method-assign]

    usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 100
    assert usage.cache_creation_tokens == 20
    assert usage.cached_tokens == 120


@pytest.mark.asyncio
async def test_bedrock_usage_missing_cache_fields_reads_zero() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1", cache_enabled=True)
    client = make_anthropic_stream_mock(
        _cache_usage_stream_events(cache_read=None, cache_creation=None)
    )
    provider._client = lambda: client  # type: ignore[method-assign]

    usage = await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    assert usage is not None
    assert usage.input_tokens == 50
    assert usage.cache_read_tokens == 0
    assert usage.cache_creation_tokens == 0
    assert usage.cached_tokens == 0


# --- Task 5: cross-provider parity ---


def _provider_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Callable[[bool], ClaudeProvider | VertexClaudeProvider | BedrockClaudeProvider]]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    return {
        "claude": lambda enabled: ClaudeProvider(cache_enabled=enabled),
        "vertex": lambda enabled: VertexClaudeProvider(
            project_id="p", cache_enabled=enabled
        ),
        "bedrock": lambda enabled: BedrockClaudeProvider(
            aws_region="us-east-1", cache_enabled=enabled
        ),
    }


@pytest.mark.parametrize("provider_key", ["claude", "vertex", "bedrock"])
@pytest.mark.asyncio
async def test_all_three_providers_identical_markers(
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = _provider_factories(monkeypatch)
    provider = factories[provider_key](True)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    if provider_key == "claude":
        patch_target = "anthropic.AsyncAnthropic"
    elif provider_key == "vertex":
        patch_target = "anthropic.AsyncAnthropicVertex"
    else:
        provider._client = lambda: client  # type: ignore[method-assign]
        patch_target = ""

    if patch_target:
        with patch(patch_target, return_value=client):
            await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)
    else:
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert captured_system == _expected_cached_system()
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("provider_key", ["claude", "vertex", "bedrock"])
@pytest.mark.asyncio
async def test_all_three_providers_disabled_identical(
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = _provider_factories(monkeypatch)
    provider = factories[provider_key](False)
    client = make_anthropic_stream_mock(_minimal_stream_events())

    if provider_key == "claude":
        patch_target = "anthropic.AsyncAnthropic"
    elif provider_key == "vertex":
        patch_target = "anthropic.AsyncAnthropicVertex"
    else:
        provider._client = lambda: client  # type: ignore[method-assign]
        patch_target = ""

    if patch_target:
        with patch(patch_target, return_value=client):
            await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)
    else:
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert captured_system == SYSTEM_TEXT
    _assert_no_cache_control(captured_system)
    _assert_no_cache_control(captured_tools)
