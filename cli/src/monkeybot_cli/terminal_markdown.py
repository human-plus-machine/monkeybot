"""Plain-text rendering for streamed assistant markdown in the terminal."""

from __future__ import annotations

import re

_HEADER = re.compile(r"^#{1,6}\s+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_BULLET = re.compile(r"^(\s*)[\*\-]\s+")
_CODE = re.compile(r"`([^`]+)`")
_ORPHAN_MARKERS = re.compile(r"\*+")


def plain_text_markdown_line(line: str) -> str:
    """Strip common inline markdown markers from a single line."""
    text = _HEADER.sub("", line)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _CODE.sub(r"\1", text)
    text = _ORPHAN_MARKERS.sub("", text)
    return text


class MarkdownPlainStream:
    """Buffer streamed assistant text and emit line-wise plain text."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._pending += chunk
        parts: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            parts.append(plain_text_markdown_line(line))
            parts.append("\n")
        return "".join(parts)

    def flush(self) -> str:
        if not self._pending:
            return ""
        rendered = plain_text_markdown_line(self._pending)
        self._pending = ""
        return rendered
