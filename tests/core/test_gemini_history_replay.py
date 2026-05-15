"""Gemini provider replay: typed ContentBlock → Vertex ``contents``."""

from __future__ import annotations

import types

import pytest

from monkeybot.core.content_blocks import Text, Thinking, ToolRequest, ToolResponse
from monkeybot.core.interfaces import LLMError
from monkeybot.core.provider import Message
from monkeybot.core.providers.gemini import (
    SYNTHETIC_THOUGHT_SIGNATURE,
    THOUGHT_SIGNATURE_KEY,
    _merge_function_call_args,
    _messages_to_contents,
)

from tests.providers.conftest import typed_messages_four_turn, typed_messages_turn_2b_tool_only


def test_gemini_tool_response_requires_tool_name() -> None:
    rest = [
        Message(
            role="user",
            content=[
                ToolResponse(id="x", tool_name="", result=[Text(text="a")]),
            ],
        ),
    ]
    with pytest.raises(LLMError, match="tool_name|Cannot replay tool result"):
        _messages_to_contents(rest)


def test_gemini_canonical_four_turn() -> None:
    rest = typed_messages_four_turn()
    contents = _messages_to_contents(rest)
    assert len(contents) == 4

    assert contents[0].role == "user"
    assert len(contents[0].parts) == 1
    assert contents[0].parts[0].text == "hi"

    assert contents[1].role == "model"
    p1 = contents[1].parts
    texts = [p.text for p in p1 if getattr(p, "text", None)]
    assert "ok" in texts
    fcs = [p.function_call for p in p1 if getattr(p, "function_call", None) is not None]
    assert len(fcs) == 1
    fc = fcs[0]
    assert fc.name == "echo"
    assert dict(fc.args) == {"x": 1}

    assert contents[2].role == "user"
    frs = [
        p.function_response
        for p in contents[2].parts
        if getattr(p, "function_response", None) is not None
    ]
    assert len(frs) == 1
    fr = frs[0]
    assert fr.name == "echo"
    assert dict(fr.response) == {"result": "done"}

    assert contents[3].role == "model"
    assert contents[3].parts[0].text == "all set"


def test_gemini_tool_only_assistant_no_text_part() -> None:
    rest = typed_messages_turn_2b_tool_only()
    contents = _messages_to_contents(rest)
    assert len(contents) == 1
    c = contents[0]
    assert c.role == "model"
    assert len(c.parts) == 1
    assert c.parts[0].function_call is not None


def _active_loop_messages() -> list[Message]:
    """Older history + active assistant turn carrying thinking + tool call."""
    return [
        Message(role="user", content=[Text(text="first")]),
        Message(
            role="assistant",
            content=[
                ToolRequest(
                    id="old",
                    name="noop",
                    args={},
                    metadata={THOUGHT_SIGNATURE_KEY: "stale-sig"},
                )
            ],
        ),
        Message(
            role="user",
            content=[ToolResponse(id="old", tool_name="noop", result=[Text(text="ok")])],
        ),
        Message(role="user", content=[Text(text="now do this")]),
        Message(
            role="assistant",
            content=[
                Thinking(thinking="reasoning step", signature="sig-think"),
                ToolRequest(
                    id="cur",
                    name="search",
                    args={"q": "x"},
                    metadata={THOUGHT_SIGNATURE_KEY: "sig-tool"},
                ),
            ],
        ),
    ]


def _has_thought_signature(part) -> str | None:
    """Return ``thought_signature`` on a ``types.Part`` if present, else ``None``.

    The google-genai SDK stores ``thought_signature`` as ``bytes`` on the wire
    (UTF-8 encoded from any string input). Decode for assertion comparison.
    """
    sig = getattr(part, "thought_signature", None)
    if isinstance(sig, bytes):
        return sig.decode("utf-8") or None
    return sig if isinstance(sig, str) and sig else None


def test_gemini_replay_signature_only_in_active_loop() -> None:
    rest = _active_loop_messages()
    contents = _messages_to_contents(rest)

    # Older assistant tool call (index 1) is outside the active loop → signature stripped.
    old_fc_part = contents[1].parts[0]
    assert old_fc_part.function_call is not None
    assert _has_thought_signature(old_fc_part) is None

    # Active assistant turn (last) keeps Thinking + signed function_call.
    active = contents[-1]
    thinking_parts = [p for p in active.parts if getattr(p, "thought", False)]
    assert len(thinking_parts) == 1
    assert thinking_parts[0].text == "reasoning step"
    assert _has_thought_signature(thinking_parts[0]) == "sig-think"

    fc_parts = [p for p in active.parts if getattr(p, "function_call", None) is not None]
    assert len(fc_parts) == 1
    assert _has_thought_signature(fc_parts[0]) == "sig-tool"


def test_gemini_replay_synthetic_signature_when_missing_on_first_active_tool_call() -> None:
    rest = [
        Message(role="user", content=[Text(text="hi")]),
        Message(
            role="assistant",
            content=[
                ToolRequest(id="c1", name="echo", args={}),  # no metadata
            ],
        ),
    ]
    contents = _messages_to_contents(rest)
    assistant = contents[1]
    fc_parts = [p for p in assistant.parts if getattr(p, "function_call", None) is not None]
    assert len(fc_parts) == 1
    assert _has_thought_signature(fc_parts[0]) == SYNTHETIC_THOUGHT_SIGNATURE


def test_gemini_replay_thinking_dropped_outside_active_loop() -> None:
    rest = [
        Message(role="user", content=[Text(text="first")]),
        Message(
            role="assistant",
            content=[Thinking(thinking="old reasoning", signature="old-sig")],
        ),
        Message(role="user", content=[Text(text="second")]),
        Message(
            role="assistant",
            content=[Thinking(thinking="new reasoning", signature="new-sig")],
        ),
    ]
    contents = _messages_to_contents(rest)
    # Older assistant turn: Thinking dropped → empty Part injected as filler.
    assert all(not getattr(p, "thought", False) for p in contents[1].parts)
    # Active assistant turn: Thinking preserved.
    thinking_in_active = [p for p in contents[-1].parts if getattr(p, "thought", False)]
    assert len(thinking_in_active) == 1
    assert thinking_in_active[0].text == "new reasoning"


def test_tool_request_metadata_roundtrip() -> None:
    block = ToolRequest(
        id="c1",
        name="echo",
        args={"x": 1},
        metadata={THOUGHT_SIGNATURE_KEY: "sig-abc"},
    )
    raw = block.to_dict()
    assert raw["metadata"] == {THOUGHT_SIGNATURE_KEY: "sig-abc"}
    restored = ToolRequest._from_dict_payload(raw)
    assert restored.metadata == {THOUGHT_SIGNATURE_KEY: "sig-abc"}


def test_tool_request_metadata_omitted_when_none() -> None:
    block = ToolRequest(id="c1", name="echo", args={})
    assert "metadata" not in block.to_dict()


def test_gemini_stream_merges_partial_function_args() -> None:
    acc: dict = {}
    acc = _merge_function_call_args(
        acc,
        types.SimpleNamespace(args={"x": 1}, partial_args=None),
    )
    acc = _merge_function_call_args(
        acc,
        types.SimpleNamespace(args=None, partial_args={"y": 2}),
    )
    acc = _merge_function_call_args(
        acc,
        types.SimpleNamespace(args=None, partial_args=" "),
    )
    assert acc == {"x": 1, "y": 2}
