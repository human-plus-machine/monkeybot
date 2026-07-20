"""OpenAI chat history conversion (`_messages_to_openai`, block-native)."""

from __future__ import annotations

import json

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
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


def test_openai_skips_thinking_blocks() -> None:
    """Ollama/HF must not crash when history contains Thinking from a prior turn."""
    m = Message(
        role="assistant",
        content=[
            Thinking(thinking="plan the tool call", signature="sig"),
            RedactedThinking(data="opaque"),
            Text(text="calling shell"),
            ToolRequest(id="c1", name="run_command", args={"argv": ["ls"]}),
        ],
    )
    _sys, rows = _messages_to_openai([m])
    assert rows == [
        {
            "role": "assistant",
            "content": "calling shell",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps({"argv": ["ls"]}, ensure_ascii=False),
                    },
                }
            ],
        }
    ]


def test_openai_tool_response_image_becomes_text_placeholder() -> None:
    """NVIDIA/OpenAI-compat tool rows are text-only; Image blocks must not raise."""
    from monkeybot.core.types.content_blocks import Image
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            ToolResponse(
                id="c1",
                tool_name="load_file",
                result=[
                    Image(
                        mime_type="image/png",
                        data="aW1n",
                        metadata={"path": "./generated-media/images/x.png"},
                    )
                ],
            )
        ],
    )
    _sys, rows = messages_to_openai([msg])
    assert rows[0]["role"] == "tool"
    assert rows[0]["tool_call_id"] == "c1"
    content = rows[0]["content"]
    assert "image loaded" in content
    assert "generated-media/images/x.png" in content
    assert "aW1n" not in content
    assert "do not invent a different subject" in content
    assert "pixels omitted" in content


def test_openai_user_image_becomes_image_url() -> None:
    """User-attached images must convert to a Chat Completions image_url block, not raise."""
    from monkeybot.core.types.content_blocks import Image
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[Text(text="what is this?"), Image(mime_type="image/png", data="aW1n")],
    )
    _sys, rows = messages_to_openai([msg])
    assert rows[0]["role"] == "user"
    content = rows[0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1n"},
    }


def test_openai_user_file_becomes_text_placeholder() -> None:
    """User-attached files (PDFs) must not raise; Chat Completions has no document type."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            Text(text="summarize this"),
            File(mime_type="application/pdf", data="cGRm", metadata={"filename": "a.pdf"}),
        ],
    )
    _sys, rows = messages_to_openai([msg])
    content = rows[0]["content"]
    assert content[0] == {"type": "text", "text": "summarize this"}
    assert content[1]["type"] == "text"
    assert "cannot read file contents" in content[1]["text"]
    assert "isn't supported here instead of guessing" in content[1]["text"]
    assert "a.pdf" in content[1]["text"]
    assert "cGRm" not in content[1]["text"]
    # Must not reuse tool-result media wording, which invites the model to
    # "describe" content it was never actually given.
    assert "describe" not in content[1]["text"].lower()
    assert "already shown in the ui" not in content[1]["text"].lower()


def test_openai_user_image_only_message() -> None:
    """Single-block image-only user message (no leading Text) must not collapse to a bare string."""
    from monkeybot.core.types.content_blocks import Image
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(role="user", content=[Image(mime_type="image/png", data="aW1n")])
    _sys, rows = messages_to_openai([msg])
    assert rows[0] == {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}}
        ],
    }


def test_openai_user_file_only_message() -> None:
    """Single-block file-only user message (no leading Text) must not raise."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[File(mime_type="application/pdf", data="cGRm", metadata={"filename": "a.pdf"})],
    )
    _sys, rows = messages_to_openai([msg])
    content = rows[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "cannot read file contents" in content[0]["text"]


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
