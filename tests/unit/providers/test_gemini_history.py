"""Gemini tool-result media: inline_data parts alongside the functionResponse.

Covers ``gemini.py::_media_parts_from_blocks`` / ``_flatten_tool_response_result``
as used from ``_messages_to_contents`` for a ``ToolResponse`` carrying an
``Image`` — previously uncovered.
"""

from __future__ import annotations

import base64

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Image, Text, ToolResponse
from monkeybot.providers.gemini import _messages_to_contents


def test_gemini_tool_result_image_ships_as_inline_data_sibling_part() -> None:
    """An Image tool result must reach the model as a real inline_data part,
    not be silently dropped (the doc's claimed behavior — verified false)."""
    rest = [
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="load_file",
                    result=[
                        Image(mime_type="image/png", data=base64.b64encode(b"pixels").decode())
                    ],
                )
            ],
        ),
    ]
    contents = _messages_to_contents(rest)
    assert len(contents) == 1
    parts = contents[0].parts

    frs = [p.function_response for p in parts if getattr(p, "function_response", None) is not None]
    assert len(frs) == 1
    assert frs[0].name == "load_file"

    inline = [p.inline_data for p in parts if getattr(p, "inline_data", None) is not None]
    assert len(inline) == 1
    assert inline[0].mime_type == "image/png"
    assert inline[0].data == b"pixels"


def test_gemini_tool_result_media_only_result_is_not_empty_string() -> None:
    """A media-only tool result must not surface as a nameless empty functionResponse."""
    rest = [
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="load_file",
                    result=[Image(mime_type="image/png", data=base64.b64encode(b"x").decode())],
                )
            ],
        ),
    ]
    contents = _messages_to_contents(rest)
    fr = next(
        p.function_response
        for p in contents[0].parts
        if getattr(p, "function_response", None) is not None
    )
    assert dict(fr.response)["result"] != ""


def test_gemini_tool_result_text_and_image_both_ship() -> None:
    rest = [
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="load_file",
                    result=[
                        Text(text="loaded"),
                        Image(mime_type="image/jpeg", data=base64.b64encode(b"jpg-bytes").decode()),
                    ],
                )
            ],
        ),
    ]
    contents = _messages_to_contents(rest)
    parts = contents[0].parts
    fr = next(
        p.function_response for p in parts if getattr(p, "function_response", None) is not None
    )
    assert dict(fr.response) == {"result": "loaded"}
    inline = [p.inline_data for p in parts if getattr(p, "inline_data", None) is not None]
    assert len(inline) == 1
    assert inline[0].mime_type == "image/jpeg"
    assert inline[0].data == b"jpg-bytes"
