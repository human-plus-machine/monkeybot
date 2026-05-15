"""Vertex Claude uses the same Anthropic message builder as direct Claude."""

from __future__ import annotations

from monkeybot.providers._utils import build_anthropic_messages

from tests.providers.conftest import typed_messages_four_turn


def test_vertex_canonical_four_turn() -> None:
    msgs = typed_messages_four_turn()
    out = build_anthropic_messages(msgs)
    assert out == [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "echo",
                    "input": {"x": 1},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "c1",
                    "content": [{"type": "text", "text": "done"}],
                },
            ],
        },
        {"role": "assistant", "content": "all set"},
    ]
