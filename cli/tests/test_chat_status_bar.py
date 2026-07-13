"""Tests for chat status / context-ring helpers."""

from __future__ import annotations

from monkeybot_cli.chat_status_bar import (
    SessionUsageView,
    UsageStore,
    format_context_ring,
    format_context_ring_markup,
    format_context_ring_plain,
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


def test_format_context_ring_plain() -> None:
    assert format_context_ring_plain(None) == "○ 0%"
    usage = SessionUsageView(estimated_prompt_tokens=250, context_window_tokens=1000)
    assert "25%" in format_context_ring_plain(usage)


def test_format_context_ring_markup_colors() -> None:
    assert format_context_ring_markup(None) == "[dim]○ 0%[/]"
    green = format_context_ring_markup(
        SessionUsageView(
            estimated_prompt_tokens=100,
            context_window_tokens=1000,
            summarization_threshold_tokens=850,
        )
    )
    assert "[green]" in green and "10%" in green

    yellow = format_context_ring_markup(
        SessionUsageView(
            estimated_prompt_tokens=700,
            context_window_tokens=1000,
            summarization_threshold_tokens=850,
        )
    )
    assert "[yellow]" in yellow and "70%" in yellow

    red = format_context_ring_markup(
        SessionUsageView(
            estimated_prompt_tokens=900,
            context_window_tokens=1000,
            summarization_threshold_tokens=850,
        )
    )
    assert "[red]" in red and "90%" in red


def test_usage_store_update() -> None:
    store = UsageStore()
    usage = SessionUsageView(estimated_prompt_tokens=100, context_window_tokens=1000)
    store.update(usage)
    assert store.usage == usage
    store.update_context_hint(estimated_prompt_tokens=400, context_window_tokens=1000)
    assert store.usage is not None
    assert store.usage.estimated_prompt_tokens == 400


def test_plain_renderer_prints_context_ring(capsys) -> None:
    from monkeybot_cli.commands.chat import _PlainRenderer
    from monkeybot_cli.chat_session import ChatSessionController

    renderer = _PlainRenderer()
    usage = SessionUsageView(
        estimated_prompt_tokens=250,
        context_window_tokens=1000,
        summarization_threshold_tokens=850,
    )
    controller = ChatSessionController(base="http://127.0.0.1:9", emit=lambda _e: None)
    renderer._on_usage_updated({"usage": usage}, controller)
    out = capsys.readouterr().out
    assert "25%" in out
    assert renderer._last_usage == usage


def test_format_voice_status() -> None:
    from monkeybot_cli.chat_status_bar import format_voice_status

    assert "listening" in format_voice_status("listening")
    assert "PTT" in format_voice_status("ptt_held")
    assert "speaking" in format_voice_status("speaking", level_db=-20.0)
