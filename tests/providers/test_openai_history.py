"""OpenAI chat history conversion (`_messages_to_openai`, block-native)."""

from __future__ import annotations

import json

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.providers.openai import _messages_to_openai
from tests.providers.conftest import typed_messages_four_turn, typed_messages_turn_2b_tool_only


def test_openai_fan_out_writes_two_tool_rows() -> None:
    msg = Message(
        role="user",
        content=[
            ToolResponse(id="x", tool_name="echo", result=[Text(text="a")]),
            ToolResponse(id="y", tool_name="echo", result=[Text(text="b")]),
        ],
    )
    _sys, rows = _messages_to_openai([msg])
    assert _sys is None
    assert rows[-2]["role"] == "tool"
    assert rows[-2]["tool_call_id"] == "x"
    assert rows[-1]["role"] == "tool"
    assert rows[-1]["tool_call_id"] == "y"


def test_openai_assistant_text_and_toolrequest() -> None:
    m = Message(
        role="assistant",
        content=[
            Text(text="ok"),
            ToolRequest(id="c1", name="echo", args={"x": 1}),
        ],
    )
    _sys, rows = _messages_to_openai([m])
    assert rows == [
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "arguments": json.dumps({"x": 1}, ensure_ascii=False),
                    },
                }
            ],
        }
    ]


def test_openai_assistant_toolrequest_only() -> None:
    m = Message(
        role="assistant",
        content=[ToolRequest(id="c2", name="ls", args={})],
    )
    _sys, rows = _messages_to_openai([m])
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "assistant"
    assert row.get("content") is None
    assert row["tool_calls"] == [
        {
            "id": "c2",
            "type": "function",
            "function": {"name": "ls", "arguments": "{}"},
        }
    ]


def test_openai_canonical_four_turn() -> None:
    msgs = typed_messages_four_turn()
    _sys, rows = _messages_to_openai(msgs)
    assert _sys is None
    assert len(rows) == 4
    assert rows[0] == {"role": "user", "content": "hi"}
    assert rows[1]["role"] == "assistant"
    assert rows[1]["content"] == "ok"
    assert rows[1]["tool_calls"][0]["function"]["name"] == "echo"
    assert json.loads(rows[1]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
    assert rows[2]["role"] == "tool"
    assert rows[2]["tool_call_id"] == "c1"
    assert rows[3] == {"role": "assistant", "content": "all set"}


def test_openai_tool_only_assistant() -> None:
    msgs = typed_messages_turn_2b_tool_only()
    _sys, rows = _messages_to_openai(msgs)
    assert _sys is None
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "assistant"
    assert row.get("content") is None
    assert len(row.get("tool_calls") or []) == 1
    assert row["tool_calls"][0]["id"] == "c2"
