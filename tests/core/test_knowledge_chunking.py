"""Tests for knowledge chunking."""

from __future__ import annotations

from monkeybot.core.knowledge.chunking import chunk_text, estimate_tokens


def test_estimate_tokens_rough() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 40) == 10


def test_chunk_text_overlap_and_prefix() -> None:
    lines = [f"line {i} content here\n" for i in range(200)]
    text = "".join(lines)
    chunks = chunk_text(
        text,
        path="src/foo.py",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.2,
    )
    assert len(chunks) >= 2
    assert chunks[0].path == "src/foo.py"
    assert chunks[0].text.startswith("src/foo.py")
    assert chunks[0].start_line == 1
    assert chunks[1].start_line < chunks[0].end_line


def test_chunk_empty() -> None:
    assert chunk_text("", path="x", source_type="note") == []
    assert chunk_text("   \n", path="x", source_type="note") == []
