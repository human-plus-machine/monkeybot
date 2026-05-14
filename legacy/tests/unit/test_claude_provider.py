"""Unit tests for ClaudeProvider — no live API required."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from monkeybot.core.provider import Message, ProviderDone, TextDelta, ToolCall
from monkeybot.providers._utils import estimate_cost

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStream:
    """Async context manager that yields a fixed sequence of fake events."""

    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __aiter__(self) -> FakeStream:
        self._iter_obj = self._aiter_impl()
        return self._iter_obj

    async def _aiter_impl(self):  # type: ignore[return]
        for e in self._events:
            yield e

    def __anext__(self):  # type: ignore[return]
        return self._iter_obj.__anext__()


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_start_event(tool_id: str, tool_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="tool_use", id=tool_id, name=tool_name),
    )


def _tool_delta_event(partial_json: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial_json),
    )


def _tool_stop_event() -> SimpleNamespace:
    return SimpleNamespace(type="content_block_stop")


def _message_start_event(input_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(usage=SimpleNamespace(input_tokens=input_tokens)),
    )


def _message_delta_event(output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_delta",
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ANTHROPIC_API_KEY is set for tests that instantiate ClaudeProvider."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------


def test_missing_api_key_raises() -> None:
    """ClaudeProvider() raises ValueError when ANTHROPIC_API_KEY is absent."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        from monkeybot.providers.claude import ClaudeProvider

        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            ClaudeProvider()


# ---------------------------------------------------------------------------
# _convert_messages tests
# ---------------------------------------------------------------------------


def test_convert_messages_tool_result() -> None:
    """role='tool' message produces tool_result content block with correct tool_use_id."""
    from monkeybot.providers.claude import ClaudeProvider

    provider = ClaudeProvider()
    messages = [
        Message(role="tool", content="42", tool_call_id="call-abc", tool_name="calculator"),
    ]
    result = provider._convert_messages(messages)
    assert len(result) == 1
    block = result[0]
    assert block["role"] == "user"
    assert block["content"][0]["type"] == "tool_result"
    assert block["content"][0]["tool_use_id"] == "call-abc"
    assert block["content"][0]["content"] == "42"


# ---------------------------------------------------------------------------
# _convert_tools tests
# ---------------------------------------------------------------------------


def test_convert_tools_uses_input_schema() -> None:
    """_convert_tools() output uses 'input_schema' key (not 'parameters')."""
    from monkeybot.core.provider import ToolDef
    from monkeybot.providers.claude import ClaudeProvider

    provider = ClaudeProvider()
    tools = [ToolDef(name="my_tool", description="does stuff", parameters={"type": "object"})]
    result = provider._convert_tools(tools)
    assert len(result) == 1
    assert "input_schema" in result[0]
    assert "parameters" not in result[0]
    assert result[0]["input_schema"] == {"type": "object"}


# ---------------------------------------------------------------------------
# stream() tests
# ---------------------------------------------------------------------------


async def _collect_stream(events: list[object]) -> list[object]:
    """Helper: patch anthropic and collect all ProviderEvent objects."""
    from monkeybot.providers.claude import ClaudeProvider

    provider = ClaudeProvider()

    mock_client = MagicMock()
    mock_client.messages.stream.return_value = FakeStream(events)

    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client
    mock_anthropic.NOT_GIVEN = None

    collected: list[object] = []
    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        async for event in await provider.stream(
            messages=[Message(role="user", content="hello")],
            tools=[],
            model="claude-3-5-haiku-20241022",
            system="",
        ):
            collected.append(event)
    return collected


async def test_stream_text_deltas_yielded_in_order() -> None:
    """Text delta events are yielded as TextDelta objects in order."""
    fake_events = [
        _text_event("Hello"),
        _text_event(", world!"),
    ]
    collected = await _collect_stream(fake_events)
    text_events = [e for e in collected if isinstance(e, TextDelta)]
    assert len(text_events) == 2
    assert text_events[0].text == "Hello"
    assert text_events[1].text == ", world!"


async def test_stream_tool_call_emitted() -> None:
    """tool_use block in stream produces a single ToolCall with correct name and args."""
    fake_events = [
        _tool_start_event("tool-123", "search"),
        _tool_delta_event('{"query": "ai news"}'),
        _tool_stop_event(),
    ]
    collected = await _collect_stream(fake_events)
    tool_calls = [e for e in collected if isinstance(e, ToolCall)]
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "search"
    assert tool_calls[0].args == {"query": "ai news"}
    assert tool_calls[0].call_id == "tool-123"


async def test_stream_provider_done_is_last() -> None:
    """ProviderDone is always the last event regardless of other events."""
    fake_events = [
        _text_event("hi"),
        _message_start_event(10),
        _message_delta_event(5),
    ]
    collected = await _collect_stream(fake_events)
    assert isinstance(collected[-1], ProviderDone)


async def test_stream_provider_done_always_emitted_empty() -> None:
    """ProviderDone is emitted even when stream yields no events."""
    collected = await _collect_stream([])
    done = [e for e in collected if isinstance(e, ProviderDone)]
    assert len(done) == 1
    assert isinstance(collected[-1], ProviderDone)


# ---------------------------------------------------------------------------
# estimate_cost tests (via _utils directly)
# ---------------------------------------------------------------------------


def test_estimate_cost_known_model() -> None:
    """estimate_cost returns correct non-zero value for known model."""
    pricing = {"my-model": (1.00, 2.00)}
    cost = estimate_cost("my-model", input_tokens=1_000_000, output_tokens=0, pricing=pricing)
    assert cost == pytest.approx(1.00)


def test_estimate_cost_unknown_model() -> None:
    """estimate_cost returns 0.0 for unknown model."""
    pricing: dict[str, tuple[float, float]] = {}
    cost = estimate_cost("unknown", input_tokens=500_000, output_tokens=500_000, pricing=pricing)
    assert cost == 0.0
