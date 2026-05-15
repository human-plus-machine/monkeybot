"""Unit tests for Anthropic message conversion (harness history → API shape)."""

from __future__ import annotations

import json

from monkeybot.core.provider import Message
from monkeybot.providers._utils import build_anthropic_messages


def _tool_placeholder(*calls: dict[str, object], pre: str = "") -> str:
    tail = json.dumps({"tool_calls": list(calls)}, ensure_ascii=False)
    return f"{pre}\n{tail}" if pre else tail


def test_user_and_assistant_passthrough() -> None:
    out = build_anthropic_messages(
        [
            Message(role="user", content="hello"),
            Message(role="assistant", content="plain reply"),
        ]
    )
    assert out == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "plain reply"},
    ]


def test_single_tool_call_no_pretext() -> None:
    content = _tool_placeholder(
        {"call_id": "c1", "name": "echo", "args": {"x": 1}},
    )
    out = build_anthropic_messages([Message(role="assistant", content=content)])
    assert out == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "echo",
                    "input": {"x": 1},
                }
            ],
        }
    ]


def test_single_tool_call_with_pretext() -> None:
    content = _tool_placeholder(
        {"call_id": "c1", "name": "echo", "args": {}},
        pre="Let me check.",
    )
    out = build_anthropic_messages([Message(role="assistant", content=content)])
    assert out[0]["role"] == "assistant"
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Let me check."}
    assert blocks[1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "echo",
        "input": {},
    }


def test_batch_tool_calls() -> None:
    content = _tool_placeholder(
        {"call_id": "a1", "name": "task", "args": {"task": "A"}},
        {"call_id": "b1", "name": "task", "args": {"task": "B"}},
        {"call_id": "c1", "name": "task", "args": {"task": "C"}},
    )
    out = build_anthropic_messages([Message(role="assistant", content=content)])
    blocks = out[0]["content"]
    assert [b["id"] for b in blocks if b["type"] == "tool_use"] == ["a1", "b1", "c1"]


def test_tool_result_maps_to_user_role() -> None:
    out = build_anthropic_messages(
        [
            Message(
                role="tool",
                content="ok",
                tool_call_id="tid",
                tool_name="run_command",
            ),
        ]
    )
    assert out == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tid",
                    "content": "ok",
                }
            ],
        }
    ]


def test_full_conversation_round_trip() -> None:
    """Every tool_result must reference a tool_use id from the prior assistant tool row."""
    tail = json.dumps(
        {
            "tool_calls": [
                {
                    "call_id": "toolu_vrtx_017VSF7NcRZJjttHEwUFtGK5",
                    "name": "search_memory",
                    "args": {"query": "don"},
                }
            ]
        },
        ensure_ascii=False,
    )
    messages = [
        Message(role="user", content="what should you call me"),
        Message(role="assistant", content=tail),
        Message(
            role="tool",
            content='{"hits": []}',
            tool_call_id="toolu_vrtx_017VSF7NcRZJjttHEwUFtGK5",
            tool_name="search_memory",
        ),
        Message(role="assistant", content="I should call you the Don."),
    ]
    out = build_anthropic_messages(messages)
    assert len(out) == 4

    assert out[0] == {"role": "user", "content": "what should you call me"}

    assert out[1]["role"] == "assistant"
    assistant_blocks = out[1]["content"]
    tool_use_ids = {
        b["id"] for b in assistant_blocks if isinstance(b, dict) and b.get("type") == "tool_use"
    }
    assert "toolu_vrtx_017VSF7NcRZJjttHEwUFtGK5" in tool_use_ids

    assert out[2]["role"] == "user"
    tr = out[2]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] in tool_use_ids

    assert out[3] == {"role": "assistant", "content": "I should call you the Don."}
