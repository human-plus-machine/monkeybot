"""Tests for the terminal chat status bar."""

from __future__ import annotations

import sys

import pytest

from monkeybot_cli.chat_status_bar import (
    ChatStatusBar,
    SessionUsageView,
    format_context_ring,
    format_status_line,
    parse_usage_response,
)


def test_parse_usage_response_fills_threshold_default() -> None:
    usage = parse_usage_response(
        {
            "session_id": "s1",
            "turns": 2,
            "input_tokens": 1200,
            "output_tokens": 340,
            "cached_tokens": 0,
            "cost_usd": 0.0123,
            "period_start": 0,
            "period_end": 1,
            "last_prompt_tokens": 900,
            "estimated_prompt_tokens": 1100,
            "context_window_tokens": 1000,
        }
    )
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 340
    assert usage.cost_usd == 0.0123
    assert usage.estimated_prompt_tokens == 1100
    assert usage.summarization_threshold_tokens == 850


def test_format_context_ring_uses_estimate_over_last_prompt() -> None:
    ring = format_context_ring(
        estimated_prompt_tokens=500,
        last_prompt_tokens=900,
        context_window_tokens=1000,
        summarization_threshold_tokens=850,
    )
    assert "50%" in ring
    assert "◕" in ring or "◑" in ring


def test_format_context_ring_falls_back_to_last_prompt() -> None:
    ring = format_context_ring(
        estimated_prompt_tokens=0,
        last_prompt_tokens=250,
        context_window_tokens=1000,
        summarization_threshold_tokens=850,
    )
    assert "25%" in ring


def test_format_status_line_shows_context_ring_only() -> None:
    usage = SessionUsageView(
        input_tokens=1234,
        output_tokens=567,
        cost_usd=0.0456,
        estimated_prompt_tokens=400,
        context_window_tokens=1000,
        summarization_threshold_tokens=850,
    )
    line = format_status_line(usage, width=120)
    assert "40%" in line
    assert "In" not in line
    assert "Out" not in line
    assert "$" not in line


def test_format_status_line_placeholder_without_usage() -> None:
    line = format_status_line(None, width=80)
    assert "0%" in line
    assert "In" not in line
    assert "Out" not in line


def test_activate_reserves_bottom_row_with_scroll_region(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "write", writes.append)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    monkeypatch.setattr(
        "monkeybot_cli.chat_status_bar.shutil.get_terminal_size",
        lambda fallback=None: type("Size", (), {"columns": 80, "lines": 24})(),
    )

    bar = ChatStatusBar()
    bar.activate()

    assert bar.active
    assert any("\x1b[1;23r" in chunk for chunk in writes)
    assert any("\x1b[24;1H" in chunk for chunk in writes)


def test_deactivate_resets_scroll_region(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "write", writes.append)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    monkeypatch.setattr(
        "monkeybot_cli.chat_status_bar.shutil.get_terminal_size",
        lambda fallback=None: type("Size", (), {"columns": 80, "lines": 24})(),
    )

    bar = ChatStatusBar()
    bar.activate()
    writes.clear()
    bar.deactivate()

    assert not bar.active
    assert any("\x1b[r" in chunk for chunk in writes)
