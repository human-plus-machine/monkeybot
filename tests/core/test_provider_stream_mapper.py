"""Tests for :mod:`monkeybot.core.runtime.provider_stream_mapper`."""

from __future__ import annotations

from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta, ToolCall, ToolInputDelta
from monkeybot.core.runtime.events import (
    AssistantDelta,
    AssistantTextEnded,
    AssistantTextStarted,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ThinkingBlockStarted,
    ToolInputDeltaEvent,
)
from monkeybot.core.runtime.provider_stream_mapper import ProviderStreamMapper


def test_mapper_emits_text_bounds_around_deltas() -> None:
    m = ProviderStreamMapper("r1")
    evs = m.map(TextDelta(text="hi"))
    assert isinstance(evs[0], AssistantTextStarted)
    assert isinstance(evs[1], AssistantDelta)
    assert evs[1].delta == "hi"
    finish = m.finish()
    assert isinstance(finish[0], AssistantTextEnded)
    assert m.assistant_text == "hi"


def test_mapper_emits_thinking_bounds() -> None:
    m = ProviderStreamMapper("r1")
    evs = m.map(ThinkingDelta(text="think", signature="sig"))
    assert isinstance(evs[0], ThinkingBlockStarted)
    assert isinstance(evs[1], ThinkingBlockDelta)
    finish = m.finish()
    assert isinstance(finish[0], ThinkingBlockComplete)
    assert finish[0].signature == "sig"


def test_mapper_tool_call_closes_blocks_and_buffers() -> None:
    m = ProviderStreamMapper("r1")
    m.map(TextDelta(text="a"))
    call = ToolCall(call_id="c1", name="read_file", args={})
    evs = m.map(call)
    assert any(isinstance(e, AssistantTextEnded) for e in evs)
    assert "c1" in m.pending


def test_mapper_tool_input_delta_and_done() -> None:
    m = ProviderStreamMapper("r1")
    evs = m.map(ToolInputDelta(call_id="c1", name="x", delta="{"))
    assert evs == [
        ToolInputDeltaEvent(request_id="r1", call_id="c1", tool="x", delta="{")
    ]
    assert m.map(Done(truncated=True)) == []
    assert m.stream_truncated is True


def test_mapper_closes_text_before_thinking_and_supports_reentry() -> None:
    """OpenAI-compat can emit TextDelta then ThinkingDelta from one chunk.

    Starting thinking must end the open text block (LIFO), and a later text
    delta must be allowed to open a fresh text block after thinking closes.
    """
    m = ProviderStreamMapper("r1")
    text1 = m.map(TextDelta(text="hello"))
    assert [type(e).__name__ for e in text1] == [
        "AssistantTextStarted",
        "AssistantDelta",
    ]

    thinking = m.map(ThinkingDelta(text="reason", signature="sig"))
    assert [type(e).__name__ for e in thinking] == [
        "AssistantTextEnded",
        "ThinkingBlockStarted",
        "ThinkingBlockDelta",
    ]

    text2 = m.map(TextDelta(text=" world"))
    assert [type(e).__name__ for e in text2] == [
        "ThinkingBlockComplete",
        "AssistantTextStarted",
        "AssistantDelta",
    ]
    assert text2[0].signature == "sig"  # type: ignore[attr-defined]

    finish = m.finish()
    assert [type(e).__name__ for e in finish] == ["AssistantTextEnded"]
    assert m.assistant_text == "hello world"
    assert m.thinking_text == "reason"
