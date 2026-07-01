"""Tests for terminal markdown plain-text rendering."""

from __future__ import annotations

from monkeybot_cli.terminal_markdown import MarkdownPlainStream, plain_text_markdown_line


def test_plain_text_markdown_line_strips_header_and_bold() -> None:
    line = "### **Core Capabilities**"
    assert plain_text_markdown_line(line) == "Core Capabilities"


def test_plain_text_markdown_line_bullet() -> None:
    line = "*   **Analysis & Reasoning:** I can think"
    assert plain_text_markdown_line(line) == "• Analysis & Reasoning: I can think"


def test_plain_text_markdown_line_numbered_list_unchanged() -> None:
    line = "1.  **browser**: Allows me to navigate"
    assert plain_text_markdown_line(line) == "1.  browser: Allows me to navigate"


def test_markdown_plain_stream_emits_on_newline() -> None:
    stream = MarkdownPlainStream()
    assert stream.feed("### **Title**\n") == "Title\n"
    assert stream.flush() == ""


def test_markdown_plain_stream_buffers_partial_line() -> None:
    stream = MarkdownPlainStream()
    assert stream.feed("**partial") == ""
    assert stream.flush() == "partial"


def test_markdown_plain_stream_multiline_message() -> None:
    stream = MarkdownPlainStream()
    raw = (
        "### **Core Capabilities**\n"
        "*   **Analysis & Reasoning:** details\n"
        "1.  **browser**: browse\n"
    )
    out = stream.feed(raw)
    assert out == (
        "Core Capabilities\n"
        "• Analysis & Reasoning: details\n"
        "1.  browser: browse\n"
    )
