"""Unit tests for Anthropic message conversion (typed ContentBlock → API shape)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text, Thinking, ToolRequest, ToolResponse
from monkeybot.providers._utils import build_anthropic_messages


def test_build_user_text_only() -> None:
    out = build_anthropic_messages([Message.text("user", "hello")])
    assert out == [{"role": "user", "content": "hello"}]


def test_build_assistant_text_and_toolrequest() -> None:
    out = build_anthropic_messages(
        [
            Message(
                role="assistant",
                content=[
                    Text(text="ok"),
                    ToolRequest(id="c1", name="echo", args={"x": 1}),
                ],
            ),
        ]
    )
    assert out == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "c1", "name": "echo", "input": {"x": 1}},
            ],
        }
    ]


def test_build_assistant_toolrequest_only() -> None:
    out = build_anthropic_messages(
        [
            Message(
                role="assistant",
                content=[ToolRequest(id="c2", name="ls", args={})],
            ),
        ]
    )
    assert out == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c2", "name": "ls", "input": {}},
            ],
        }
    ]


def test_build_user_toolresponse_pair_fanin() -> None:
    out = build_anthropic_messages(
        [
            Message(
                role="user",
                content=[
                    ToolResponse(id="x", tool_name="echo", result=[Text(text="a")]),
                    ToolResponse(id="y", tool_name="echo", result=[Text(text="b")]),
                ],
            ),
        ]
    )
    assert out == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "x",
                    "content": [{"type": "text", "text": "a"}],
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "y",
                    "content": [{"type": "text", "text": "b"}],
                },
            ],
        }
    ]


def test_build_thinking_block() -> None:
    out = build_anthropic_messages(
        [
            Message(
                role="assistant",
                content=[Thinking(thinking="t", signature="sig")],
            ),
        ]
    )
    assert out == [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "t", "signature": "sig"},
            ],
        }
    ]


def test_rejects_disallowed_tool_role() -> None:
    bad = cast(
        Message,
        SimpleNamespace(role="tool", content=[Text(text="x")]),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="tool"):
        build_anthropic_messages([bad])
