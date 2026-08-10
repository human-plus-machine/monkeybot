"""Tests for Anthropic cache_control markers and cache usage field mapping."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monkeybot.core.context.epoch import SYSTEM_CONTEXT_UPDATE_HEADING
from monkeybot.core.llm.provider import Message, ProviderCallHints, UsageEvent
from monkeybot.core.prompts.headings import (
    CURRENT_DATE_HEADING,
    CURRENT_REQUEST_HEADING,
    MEMORY_INDEX_HEADING,
    MEMORY_NUDGE_HEADING,
    RUNTIME_NOTES_HEADING,
    SKILLS_HEADING,
    TODO_LIST_HEADING,
    VOLATILE_SECTION_HEADINGS,
    VOLATILE_SECTION_MARKERS,
    heading_marker,
)
from monkeybot.providers._utils import (
    build_cached_system_blocks,
    mark_conversation_cache_breakpoints,
    mark_last_tool_cached,
    prepare_anthropic_cached_payload,
    split_system_prompt_for_cache,
)
from monkeybot.providers.bedrock import BedrockClaudeProvider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider
from tests.providers.conftest import (
    CANONICAL_TOOL_DEFS,
    make_anthropic_stream_mock,
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


# --- Task 1: _utils helpers ---


def test_build_cached_system_blocks_shape() -> None:
    assert build_cached_system_blocks("SYS") == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]


def test_volatile_markers_cover_every_heading_constant() -> None:
    """Every volatile heading must be detectable by the cache splitter.

    The markers used to be literals duplicated by hand in ``providers._utils``
    (the owning modules cannot be imported there: ``core.context`` ->
    ``core.config.settings`` -> ``providers.claude`` cycles). They drifted the
    first time a heading gained a prose body, silently pushing volatile text
    into the cached prefix. Both sides now share ``core.prompts.headings``; this
    test keeps a heading from being added there without joining the marker set.
    """
    for heading in (
        CURRENT_DATE_HEADING,
        MEMORY_INDEX_HEADING,
        MEMORY_NUDGE_HEADING,
        SKILLS_HEADING,
        TODO_LIST_HEADING,
        CURRENT_REQUEST_HEADING,
        RUNTIME_NOTES_HEADING,
        SYSTEM_CONTEXT_UPDATE_HEADING,
    ):
        assert heading in VOLATILE_SECTION_HEADINGS
        stable, volatile = split_system_prompt_for_cache(f"STABLE{heading}body")
        assert stable == "STABLE"
        assert volatile.startswith(heading)


def test_volatile_markers_ignore_prose_under_the_heading() -> None:
    """A marker is the title line only, so heading prose can be reworded freely."""
    for marker in VOLATILE_SECTION_MARKERS:
        assert marker.startswith("\n\n## ")
        assert marker.count("\n") == 3, marker  # two leading, one closing the title
    assert heading_marker("\n\n## Memory index\nprose here\n") == "\n\n## Memory index\n"


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


def test_build_cached_system_blocks_long_uses_1h_ttl() -> None:
    blocks = build_cached_system_blocks("SYS", cache_retention="long")
    assert blocks == [
        {
            "type": "text",
            "text": "SYS",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


def test_cache_retention_none_emits_no_markers() -> None:
    blocks = build_cached_system_blocks("SYS", cache_retention="none")
    assert blocks == [{"type": "text", "text": "SYS"}]
    tools = mark_last_tool_cached(
        [{"name": "a", "description": "d", "input_schema": {}}],
        cache_retention="none",
    )
    assert "cache_control" not in tools[0]
    msgs = mark_conversation_cache_breakpoints(
        [{"role": "user", "content": "hi"}],
        cache_retention="none",
    )
    assert msgs == [{"role": "user", "content": "hi"}]


def _count_cache_controls(obj: object) -> int:
    if isinstance(obj, dict):
        n = 1 if "cache_control" in obj else 0
        return n + sum(_count_cache_controls(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_cache_controls(v) for v in obj)
    return 0


def test_mark_conversation_cache_breakpoints_marks_last_two() -> None:
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    marked = mark_conversation_cache_breakpoints(messages, max_breakpoints=2)
    assert messages[0] == {"role": "user", "content": "u1"}  # input untouched
    assert marked[0] == {"role": "user", "content": "u1"}
    assert marked[1]["content"] == [
        {"type": "text", "text": "a1", "cache_control": {"type": "ephemeral"}}
    ]
    assert marked[2]["content"] == [
        {"type": "text", "text": "u2", "cache_control": {"type": "ephemeral"}}
    ]


def test_mark_conversation_cache_breakpoints_advances_with_turns() -> None:
    turn1 = mark_conversation_cache_breakpoints(
        [{"role": "user", "content": "u1"}],
        max_breakpoints=2,
    )
    turn2 = mark_conversation_cache_breakpoints(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
        max_breakpoints=2,
    )
    # Newest marked block advances from u1 -> u2.
    assert turn1[0]["content"][0]["text"] == "u1"
    assert turn2[-1]["content"][0]["text"] == "u2"
    assert "cache_control" in turn2[-1]["content"][0]
    assert "cache_control" in turn2[-2]["content"][0]


def test_prepare_payload_total_breakpoints_within_limit() -> None:
    import anthropic

    msgs = [
        Message.text("user", "u1"),
        Message.text("assistant", "a1"),
        Message.text("user", "u2"),
    ]
    system_param, converted, tools_param = prepare_anthropic_cached_payload(
        system="SYS",
        messages=msgs,
        tools=_TWO_TOOLS,
        cache_retention="short",
        not_given=anthropic.NOT_GIVEN,
    )
    total = (
        _count_cache_controls(system_param)
        + _count_cache_controls(converted)
        + _count_cache_controls(tools_param)
    )
    assert _count_cache_controls(converted) >= 1
    assert total <= 4
    assert total == 4  # 1 system + 1 tools + 2 conversation


# --- Task 2: ClaudeProvider ---


@pytest.mark.asyncio
async def test_claude_enabled_system_is_cached_block_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider()
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
    provider = ClaudeProvider()
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropic", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_enabled_empty_system_uses_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    provider = ClaudeProvider()
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
    provider = ClaudeProvider()
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
    provider = ClaudeProvider()
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
    provider = ClaudeProvider()
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
    provider = ClaudeProvider()
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
    provider = VertexClaudeProvider(project_id="p", region="us-east5")
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
    provider = VertexClaudeProvider(project_id="p")
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    assert captured_system == _expected_cached_system()


@pytest.mark.asyncio
async def test_vertex_enabled_last_tool_marked() -> None:
    provider = VertexClaudeProvider(project_id="p")
    client = make_anthropic_stream_mock(_minimal_stream_events())

    with patch("anthropic.AsyncAnthropicVertex", return_value=client):
        await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_vertex_usage_maps_cache_read_and_creation() -> None:
    provider = VertexClaudeProvider(project_id="p")
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
    provider = VertexClaudeProvider(project_id="p")
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
    provider = BedrockClaudeProvider(aws_region="us-east-1")
    client = make_anthropic_stream_mock(_minimal_stream_events())
    provider._client = lambda: client  # type: ignore[method-assign]

    await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_system = client.messages.stream.call_args.kwargs["system"]
    assert captured_system == _expected_cached_system()


@pytest.mark.asyncio
async def test_bedrock_enabled_last_tool_marked() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1")
    client = make_anthropic_stream_mock(_minimal_stream_events())
    provider._client = lambda: client  # type: ignore[method-assign]

    await _usage_from_stream(provider, _messages_with_system(), _TWO_TOOLS)

    captured_tools = client.messages.stream.call_args.kwargs["tools"]
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_bedrock_usage_maps_cache_read_and_creation() -> None:
    provider = BedrockClaudeProvider(aws_region="us-east-1")
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
    provider = BedrockClaudeProvider(aws_region="us-east-1")
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
) -> dict[str, ClaudeProvider | VertexClaudeProvider | BedrockClaudeProvider]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    return {
        "claude": ClaudeProvider(),
        "vertex": VertexClaudeProvider(project_id="p"),
        "bedrock": BedrockClaudeProvider(aws_region="us-east-1"),
    }


@pytest.mark.parametrize("provider_key", ["claude", "vertex", "bedrock"])
@pytest.mark.asyncio
async def test_all_three_providers_identical_markers(
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = _provider_factories(monkeypatch)
    provider = factories[provider_key]
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
    captured_messages = client.messages.stream.call_args.kwargs["messages"]
    assert captured_system == _expected_cached_system()
    assert "cache_control" not in captured_tools[0]
    assert captured_tools[1]["cache_control"] == {"type": "ephemeral"}
    assert _count_cache_controls(captured_messages) >= 1
    total = (
        _count_cache_controls(captured_system)
        + _count_cache_controls(captured_tools)
        + _count_cache_controls(captured_messages)
    )
    assert total <= 4


@pytest.mark.parametrize("provider_key", ["claude", "vertex", "bedrock"])
@pytest.mark.asyncio
async def test_all_three_providers_conversation_markers_advance(
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = _provider_factories(monkeypatch)
    provider = factories[provider_key]
    client = make_anthropic_stream_mock(_minimal_stream_events())

    if provider_key == "claude":
        patch_target = "anthropic.AsyncAnthropic"
    elif provider_key == "vertex":
        patch_target = "anthropic.AsyncAnthropicVertex"
    else:
        provider._client = lambda: client  # type: ignore[method-assign]
        patch_target = ""

    short = [
        Message.text("system", SYSTEM_TEXT),
        Message.text("user", "u1"),
    ]
    longer = [
        Message.text("system", SYSTEM_TEXT),
        Message.text("user", "u1"),
        Message.text("assistant", "a1"),
        Message.text("user", "u2"),
    ]

    async def _capture(messages: list[Message]) -> list[dict[str, Any]]:
        if patch_target:
            with patch(patch_target, return_value=client):
                await _usage_from_stream(provider, messages, _ONE_TOOL)
        else:
            await _usage_from_stream(provider, messages, _ONE_TOOL)
        return client.messages.stream.call_args.kwargs["messages"]

    msgs_short = await _capture(short)
    msgs_long = await _capture(longer)
    assert _count_cache_controls(msgs_short) >= 1
    assert _count_cache_controls(msgs_long) >= 1
    # Newest marked text advances as turns are appended.
    short_text = msgs_short[-1]["content"][0]["text"]
    long_text = msgs_long[-1]["content"][0]["text"]
    assert short_text == "u1"
    assert long_text == "u2"


@pytest.mark.parametrize("provider_key", ["claude", "vertex", "bedrock"])
@pytest.mark.asyncio
async def test_all_three_providers_retention_none_zero_markers(
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = _provider_factories(monkeypatch)
    provider = factories[provider_key]
    client = make_anthropic_stream_mock(_minimal_stream_events())
    hints = ProviderCallHints(cache_retention="none")

    if provider_key == "claude":
        patch_target = "anthropic.AsyncAnthropic"
    elif provider_key == "vertex":
        patch_target = "anthropic.AsyncAnthropicVertex"
    else:
        provider._client = lambda: client  # type: ignore[method-assign]
        patch_target = ""

    async def _run() -> None:
        async for _ in provider.stream(
            _messages_with_system(),
            _TWO_TOOLS,
            model="claude-3-5-sonnet-20241022",
            hints=hints,
        ):
            pass

    if patch_target:
        with patch(patch_target, return_value=client):
            await _run()
    else:
        await _run()

    kwargs = client.messages.stream.call_args.kwargs
    assert _count_cache_controls(kwargs["system"]) == 0
    assert _count_cache_controls(kwargs["tools"]) == 0
    assert _count_cache_controls(kwargs["messages"]) == 0


@pytest.mark.parametrize("provider_key", ["claude", "vertex", "bedrock"])
@pytest.mark.asyncio
async def test_all_three_providers_long_retention_uses_1h_ttl(
    provider_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factories = _provider_factories(monkeypatch)
    provider = factories[provider_key]
    client = make_anthropic_stream_mock(_minimal_stream_events())
    hints = ProviderCallHints(cache_retention="long")

    if provider_key == "claude":
        patch_target = "anthropic.AsyncAnthropic"
    elif provider_key == "vertex":
        patch_target = "anthropic.AsyncAnthropicVertex"
    else:
        provider._client = lambda: client  # type: ignore[method-assign]
        patch_target = ""

    async def _run() -> None:
        async for _ in provider.stream(
            _messages_with_system(),
            _ONE_TOOL,
            model="claude-3-5-sonnet-20241022",
            hints=hints,
        ):
            pass

    if patch_target:
        with patch(patch_target, return_value=client):
            await _run()
    else:
        await _run()

    kwargs = client.messages.stream.call_args.kwargs
    expected = {"type": "ephemeral", "ttl": "1h"}
    assert kwargs["system"][0]["cache_control"] == expected
    assert kwargs["tools"][0]["cache_control"] == expected
    assert kwargs["messages"][-1]["content"][0]["cache_control"] == expected
