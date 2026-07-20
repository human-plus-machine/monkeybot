"""Tests for in-memory tool-turn integrity repair."""

from __future__ import annotations

import logging

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.messages.tool_integrity import repair_tool_turn_integrity
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.providers._openai_compat import messages_to_openai
from monkeybot.providers._utils import build_anthropic_messages
from monkeybot.providers.gemini import _messages_to_contents
from tests.providers.conftest import typed_messages_four_turn


def test_repair_synthesizes_missing_tool_result() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="echo", args={"x": 1})],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    assert len(repaired) == 2
    assert repaired[0].role == "assistant"
    assert repaired[1].role == "user"
    responses = [b for b in repaired[1].content if isinstance(b, ToolResponse)]
    assert len(responses) == 1
    assert responses[0].id == "c1"
    assert responses[0].is_error is True


def test_repair_skips_synthetic_assistant_for_unresolvable_orphan() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(id="orph", tool_name="", result=[Text(text="done")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    assert len(repaired) == 1
    assert repaired[0].role == "user"
    responses = [b for b in repaired[0].content if isinstance(b, ToolResponse)]
    assert len(responses) == 1
    assert responses[0].tool_name == "unknown_tool"


def test_repair_synthesizes_assistant_for_resolvable_orphan() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(id="orph", tool_name="echo", result=[Text(text="done")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    assert len(repaired) == 2
    assert repaired[0].role == "assistant"
    requests = [b for b in repaired[0].content if isinstance(b, ToolRequest)]
    assert len(requests) == 1
    assert requests[0].id == "orph"
    assert requests[0].name == "echo"


def test_repair_fills_empty_tool_name_from_request() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="echo", args={})],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(id="c1", tool_name="", result=[Text(text="ok")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    responses = [b for b in repaired[1].content if isinstance(b, ToolResponse)]
    assert responses[0].tool_name == "echo"


def test_repair_adds_missing_result_when_user_has_partial_responses() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                ToolRequest(id="a", name="one", args={}),
                ToolRequest(id="b", name="two", args={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(id="a", tool_name="one", result=[Text(text="ok")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    responses = [b for b in repaired[1].content if isinstance(b, ToolResponse)]
    assert {b.id for b in responses} == {"a", "b"}
    missing = next(b for b in responses if b.id == "b")
    assert missing.is_error is True


def test_repair_synthesizes_orphan_in_mixed_user_turn() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ToolRequest(id="a", name="one", args={})],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(id="a", tool_name="one", result=[Text(text="ok")]),
                ToolResponse(id="c", tool_name="three", result=[Text(text="extra")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    assert repaired[0].role == "assistant"
    assert repaired[1].role == "assistant"
    orphan_req = [b for b in repaired[1].content if isinstance(b, ToolRequest)][0]
    assert orphan_req.id == "c"
    assert repaired[2].role == "user"


def test_repair_canonical_history_unchanged() -> None:
    original = typed_messages_four_turn()
    repaired = repair_tool_turn_integrity(original)
    assert len(repaired) == len(original)
    for before, after in zip(original, repaired, strict=True):
        assert before.role == after.role
        assert len(before.content) == len(after.content)


def test_repaired_orphan_history_converts_for_gemini() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(id="x", tool_name="echo", result=[Text(text="a")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    contents = _messages_to_contents(repaired)
    assert len(contents) == 2


def test_repaired_orphan_history_converts_for_anthropic() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(id="x", tool_name="echo", result=[Text(text="a")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    converted = build_anthropic_messages(repaired)
    assert len(converted) == 2


async def test_repaired_orphan_history_converts_for_openai() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ToolResponse(id="x", tool_name="echo", result=[Text(text="a")]),
            ],
        ),
    ]
    repaired = repair_tool_turn_integrity(messages)
    _system, converted = await messages_to_openai(repaired)
    assert len(converted) == 2


def test_repair_logs_synthetic_tool_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = [
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="echo", args={})],
        ),
    ]
    with caplog.at_level(logging.WARNING, logger="monkeybot.core.messages.tool_integrity"):
        repair_tool_turn_integrity(messages)
    assert any("tool_integrity_repair" in r.getMessage() for r in caplog.records)
