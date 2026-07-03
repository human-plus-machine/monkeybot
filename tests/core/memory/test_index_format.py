"""Tests for memory index format helpers."""

from __future__ import annotations

from monkeybot.core.memory.index_format import (
    append_index_entries,
    apply_index_entry_cap,
    format_index_document,
    memory_window_slice,
    parse_index_entry_lines,
)


def test_append_index_entries_preserves_recency_order() -> None:
    raw = "# Memory Index\n\n- [[episodic/old.md]] | tags: a | old\n"
    out = append_index_entries(raw, ["- [[episodic/new.md]] | tags: b | new"])
    lines = parse_index_entry_lines(out)
    assert lines == [
        "- [[episodic/old.md]] | tags: a | old",
        "- [[episodic/new.md]] | tags: b | new",
    ]


def test_apply_index_entry_cap_keeps_recent() -> None:
    lines = [f"line-{i}" for i in range(5)]
    kept, archived = apply_index_entry_cap(lines, 3)
    assert kept == ["line-2", "line-3", "line-4"]
    assert archived == ["line-0", "line-1"]


def test_memory_window_slice_returns_tail() -> None:
    lines = ["a", "b", "c", "d"]
    assert memory_window_slice(lines, 2) == ["c", "d"]


def test_format_index_document_round_trip() -> None:
    doc = format_index_document("# Memory Index", ["- [[x]] | tags: t | s"])
    assert parse_index_entry_lines(doc) == ["- [[x]] | tags: t | s"]
