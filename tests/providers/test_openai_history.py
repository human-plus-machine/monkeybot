"""OpenAI chat history conversion (`_messages_to_openai`, block-native)."""

from __future__ import annotations

import base64
import io
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
from tests.providers.conftest import (
    typed_messages_four_turn,
    typed_messages_four_turn_image_tool_result,
    typed_messages_turn_2b_tool_only,
)


def _pdf_bytes(text: str | None = "Hello World") -> bytes:
    """Build a minimal real one-page PDF, with a text layer unless *text* is None."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if text is not None:
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        resources = DictionaryObject()
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 24 Tf 10 100 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_b64(text: str | None = "Hello World") -> str:
    return base64.b64encode(_pdf_bytes(text)).decode()


async def test_openai_fan_out_writes_two_tool_rows() -> None:
    msg = Message(
        role="user",
        content=[
            ToolResponse(id="x", tool_name="echo", result=[Text(text="a")]),
            ToolResponse(id="y", tool_name="echo", result=[Text(text="b")]),
        ],
    )
    _sys, rows = await _messages_to_openai([msg])
    assert _sys is None
    assert rows[-2]["role"] == "tool"
    assert rows[-2]["tool_call_id"] == "x"
    assert rows[-1]["role"] == "tool"
    assert rows[-1]["tool_call_id"] == "y"


async def test_openai_assistant_text_and_toolrequest() -> None:
    m = Message(
        role="assistant",
        content=[
            Text(text="ok"),
            ToolRequest(id="c1", name="echo", args={"x": 1}),
        ],
    )
    _sys, rows = await _messages_to_openai([m])
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


async def test_openai_assistant_toolrequest_only() -> None:
    m = Message(
        role="assistant",
        content=[ToolRequest(id="c2", name="ls", args={})],
    )
    _sys, rows = await _messages_to_openai([m])
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


async def test_openai_skips_thinking_blocks() -> None:
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
    _sys, rows = await _messages_to_openai([m])
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


async def test_openai_tool_response_image_promotes_pixels_to_synthetic_user_row() -> None:
    """Tool rows are text-only; the pixels ride a synthetic user row appended after."""
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
    _sys, rows = await messages_to_openai([msg])
    assert len(rows) == 2
    assert rows[0]["role"] == "tool"
    assert rows[0]["tool_call_id"] == "c1"
    tool_content = rows[0]["content"]
    assert "image loaded" in tool_content
    assert "generated-media/images/x.png" in tool_content
    assert "aW1n" not in tool_content
    assert "pixels omitted" not in tool_content
    assert "do not invent a different subject" not in tool_content

    assert rows[1]["role"] == "user"
    assert rows[1]["content"] == [
        {"type": "text", "text": "Image result from tool call c1:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}},
    ]


async def test_openai_tool_response_fan_out_promotes_one_merged_row() -> None:
    """Two image tool results in one message must not insert a user row between the tool rows."""
    from monkeybot.core.types.content_blocks import Image
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            ToolResponse(id="x", tool_name="load_file", result=[Text(text="ok")]),
            ToolResponse(
                id="y",
                tool_name="load_file",
                result=[Image(mime_type="image/png", data="eQ==")],
            ),
            ToolResponse(
                id="z",
                tool_name="load_file",
                result=[Image(mime_type="image/jpeg", data="eg==")],
            ),
        ],
    )
    _sys, rows = await messages_to_openai([msg])
    assert len(rows) == 4
    assert [r["role"] for r in rows] == ["tool", "tool", "tool", "user"]
    assert [r["tool_call_id"] for r in rows[:3]] == ["x", "y", "z"]
    promoted = rows[3]
    assert promoted["content"] == [
        {"type": "text", "text": "Image result from tool call y:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,eQ=="}},
        {"type": "text", "text": "Image result from tool call z:"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eg=="}},
    ]


async def test_openai_user_image_becomes_image_url() -> None:
    """User-attached images must convert to a Chat Completions image_url block, not raise."""
    from monkeybot.core.types.content_blocks import Image
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[Text(text="what is this?"), Image(mime_type="image/png", data="aW1n")],
    )
    _sys, rows = await messages_to_openai([msg])
    assert rows[0]["role"] == "user"
    content = rows[0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1n"},
    }


async def test_openai_user_file_becomes_text_placeholder() -> None:
    """Unparseable file bytes must not raise; falls back to the can't-read placeholder."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            Text(text="summarize this"),
            File(mime_type="application/pdf", data="cGRm", metadata={"filename": "a.pdf"}),
        ],
    )
    _sys, rows = await messages_to_openai([msg])
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


async def test_openai_user_pdf_with_text_layer_sends_extracted_text() -> None:
    """A real PDF with a text layer must reach the model as actual text, not a placeholder."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            Text(text="summarize this"),
            File(
                mime_type="application/pdf",
                data=_pdf_b64("Hello World"),
                metadata={"filename": "a.pdf"},
            ),
        ],
    )
    _sys, rows = await messages_to_openai([msg])
    content = rows[0]["content"]
    assert content[1]["type"] == "text"
    assert "Hello World" in content[1]["text"]
    assert "cannot read file contents" not in content[1]["text"]


async def test_openai_tool_response_pdf_with_text_layer_sends_extracted_text() -> None:
    """A model-initiated load_file on a PDF must also get real text, not the media placeholder."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            ToolResponse(
                id="c1",
                tool_name="load_file",
                result=[
                    File(
                        mime_type="application/pdf",
                        data=_pdf_b64("Hello World"),
                        metadata={"path": "./workspace/a.pdf"},
                    )
                ],
            )
        ],
    )
    _sys, rows = await messages_to_openai([msg])
    content = rows[0]["content"]
    assert "Hello World" in content
    assert "pixels omitted" not in content


async def test_openai_user_pdf_without_text_layer_falls_back_to_placeholder() -> None:
    """A scanned/image-only PDF (no text layer) must fall back to the can't-read placeholder."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[
            Text(text="summarize this"),
            File(mime_type="application/pdf", data=_pdf_b64(None), metadata={"filename": "a.pdf"}),
        ],
    )
    _sys, rows = await messages_to_openai([msg])
    content = rows[0]["content"]
    assert "cannot read file contents" in content[1]["text"]


async def test_extract_pdf_text_truncates_long_text() -> None:
    """Extraction caps output so a huge PDF can't blow the context window."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import _extract_pdf_text

    long_text = "word " * 5000  # well over a small max_chars cap
    block = File(mime_type="application/pdf", data=_pdf_b64(long_text))
    result = await _extract_pdf_text(block, max_chars=100)
    assert result is not None
    assert len(result) < len(long_text)
    assert "[... PDF text truncated ...]" in result


async def test_openai_user_image_only_message() -> None:
    """Single-block image-only user message (no leading Text) must not collapse to a bare string."""
    from monkeybot.core.types.content_blocks import Image
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(role="user", content=[Image(mime_type="image/png", data="aW1n")])
    _sys, rows = await messages_to_openai([msg])
    assert rows[0] == {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}}],
    }


async def test_openai_user_file_only_message() -> None:
    """Single-block file-only user message (no leading Text) must not raise."""
    from monkeybot.core.types.content_blocks import File
    from monkeybot.providers._openai_compat import messages_to_openai

    msg = Message(
        role="user",
        content=[File(mime_type="application/pdf", data="cGRm", metadata={"filename": "a.pdf"})],
    )
    _sys, rows = await messages_to_openai([msg])
    content = rows[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "cannot read file contents" in content[0]["text"]


async def test_openai_canonical_four_turn() -> None:
    msgs = typed_messages_four_turn()
    _sys, rows = await _messages_to_openai(msgs)
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


async def test_openai_token_count_bounded_for_promoted_image() -> None:
    """A promoted image_url must not be tokenized as JSON text — that would count a
    1MB screenshot's base64 data URL at ~1 token per 4 chars (~340K tokens) and
    trigger spurious compaction."""
    import tiktoken

    from monkeybot.providers._openai_compat import openai_messages_token_count

    huge_b64 = "A" * 1_000_000
    rows = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image result from tool call c1:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge_b64}"}},
            ],
        }
    ]
    enc = tiktoken.get_encoding("cl100k_base")
    count = openai_messages_token_count(enc, rows)
    assert count < 1_000


async def test_openai_canonical_four_turn_image_tool_result_inserts_promoted_row() -> None:
    """The canonical four-turn shape, but the tool result is an image: five OpenAI
    rows (one more than the text-result variant) because the promoted media row
    is appended after the tool row, before the closing assistant turn."""
    msgs = typed_messages_four_turn_image_tool_result()
    _sys, rows = await _messages_to_openai(msgs)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "user", "assistant"]
    assert rows[2]["tool_call_id"] == "c1"
    assert rows[3]["content"] == [
        {"type": "text", "text": "Image result from tool call c1:"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}},
    ]
    assert rows[4] == {"role": "assistant", "content": "a screenshot"}


async def test_openai_tool_only_assistant() -> None:
    msgs = typed_messages_turn_2b_tool_only()
    _sys, rows = await _messages_to_openai(msgs)
    assert _sys is None
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "assistant"
    assert row.get("content") is None
    assert len(row.get("tool_calls") or []) == 1
    assert row["tool_calls"][0]["id"] == "c2"
