"""Gemini provider replay: tool rows must resolve to a non-empty ``FunctionResponse.name``."""

from __future__ import annotations

import json

import pytest
from monkeybot.core.provider import Message
from monkeybot.core.providers.gemini import _enrich_tool_messages, _messages_to_contents


def _assistant_with_tools(text: str, calls: list[dict]) -> Message:
    tail = json.dumps({"tool_calls": calls}, ensure_ascii=False)
    content = f"{text}\n{tail}" if text else tail
    return Message(role="assistant", content=content)


def test_enrich_tool_messages_fills_name_from_placeholder() -> None:
    assistant = _assistant_with_tools(
        "ok",
        [{"call_id": "c1", "name": "echo", "args": {}}],
    )
    tool = Message(
        role="tool",
        content='{"result":"x"}',
        tool_call_id="c1",
        tool_name=None,
    )
    rest = _enrich_tool_messages([assistant, tool])
    assert rest[1].tool_name == "echo"
    _messages_to_contents(rest)


def test_messages_to_contents_raises_when_tool_name_unresolvable() -> None:
    from monkeybot.core.interfaces import LLMError

    assistant = Message(role="assistant", content="no tools here")
    tool = Message(
        role="tool",
        content="{}",
        tool_call_id="missing",
        tool_name=None,
    )
    rest = _enrich_tool_messages([assistant, tool])
    with pytest.raises(LLMError, match="tool_name"):
        _messages_to_contents(rest)
