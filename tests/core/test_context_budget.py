"""Tests for dynamic context budgeting of tool results."""

from __future__ import annotations

import json

from monkeybot.core.runtime.context_budget import ContextBudgeter, estimate_tokens
from monkeybot.core.types.content_blocks import Text, ToolResponse


def test_estimate_tokens_char_div_four() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0


def test_fit_returns_whole_when_headroom_large() -> None:
    budgeter = ContextBudgeter(window_tokens=200_000, used_tokens=10_000, safety_fraction=0.8)
    text = "hello " * 100
    blocks = [
        ToolResponse(id="1", tool_name="run_command", result=[Text(text=text)]),
    ]
    trimmed, needs = budgeter.fit_content_blocks(blocks)
    assert not needs
    assert isinstance(trimmed[0], ToolResponse)
    assert _text(trimmed[0]) == text


def test_fit_trims_when_headroom_tight() -> None:
    budgeter = ContextBudgeter(
        window_tokens=10_000,
        used_tokens=9_500,
        safety_fraction=0.8,
        floor_tokens=500,
    )
    text = "x" * 40_000
    blocks = [
        ToolResponse(id="1", tool_name="run_command", result=[Text(text=text)]),
    ]
    trimmed, needs = budgeter.fit_content_blocks(blocks)
    assert needs
    out = _text(trimmed[0])
    assert len(out) < len(text)
    assert "truncated for context budget" in out


def test_fit_preserves_inventory_note_when_trimming() -> None:
    budgeter = ContextBudgeter(
        window_tokens=10_000,
        used_tokens=9_000,
        safety_fraction=0.5,
        floor_tokens=500,
    )
    body = "y" * 30_000
    note = "[Spill inventory — 30000 total chars, 1 total lines.\nFull output at: .monkeybot/spill/t/x.txt — use read_file with offset/limit to page through it.]"
    text = f"{body}\n{note}"
    blocks = [
        ToolResponse(id="1", tool_name="run_command", result=[Text(text=text)]),
    ]
    trimmed, _ = budgeter.fit_content_blocks(blocks)
    out = _text(trimmed[0])
    assert "Spill inventory" in out
    assert ".monkeybot/spill/t/x.txt" in out


def test_batch_allocation_splits_headroom() -> None:
    budgeter = ContextBudgeter(
        window_tokens=20_000,
        used_tokens=18_000,
        safety_fraction=0.8,
        floor_tokens=1000,
    )
    blocks = [
        ToolResponse(id="1", tool_name="a", result=[Text(text="a" * 20_000)]),
        ToolResponse(id="2", tool_name="b", result=[Text(text="b" * 20_000)]),
    ]
    trimmed, needs = budgeter.fit_content_blocks(blocks)
    assert needs
    assert len(trimmed) == 2
    assert len(_text(trimmed[0])) < 20_000
    assert len(_text(trimmed[1])) < 20_000


def test_fit_applies_log_shaping_under_pressure() -> None:
    from monkeybot.core.runtime.context_budget import ContextBudgeter

    long_log = "\n".join(f"line {i}" for i in range(500))
    budgeter = ContextBudgeter(
        window_tokens=100_000,
        used_tokens=75_000,
        pressure_tier="moderate",
    )
    blocks = [
        ToolResponse(id="1", tool_name="run_command", result=[Text(text=long_log)]),
    ]
    trimmed, _ = budgeter.fit_content_blocks(blocks)
    out = trimmed[0].result[0].text  # type: ignore[union-attr]
    assert len(out.splitlines()) < 500


def _text(block: ToolResponse) -> str:
    parts: list[str] = []
    for b in block.result:
        if isinstance(b, Text):
            parts.append(b.text)
    return "".join(parts)


def test_compute_context_pressure_tier_thresholds() -> None:
    from monkeybot.core.runtime.context_budget import (
        SUMMARY_TRIGGER_RATIO,
        compute_context_pressure_tier,
    )

    assert compute_context_pressure_tier(40_000, 100_000) is None
    assert compute_context_pressure_tier(55_000, 100_000) == "light"
    assert compute_context_pressure_tier(75_000, 100_000) == "moderate"
    assert compute_context_pressure_tier(90_000, 100_000) == "aggressive"
    # Summarization fires after aggressive shaping.
    assert SUMMARY_TRIGGER_RATIO == 0.95


def test_for_window_tightens_safety_fraction_under_pressure() -> None:
    from monkeybot.core.runtime.context_budget import ContextBudgeter

    budgeter = ContextBudgeter.for_window(window_tokens=100_000, used_tokens=75_000)
    assert budgeter.pressure_tier == "moderate"
    assert budgeter.safety_fraction <= 0.45


def test_fit_preserves_read_file_content_through_budgeter() -> None:
    """read_file JSON must not lose the content field to ingress redaction."""
    body = "     1|<!DOCTYPE html>\n" + ("     2|<p>hello</p>\n" * 400)
    payload = json.dumps(
        {
            "ok": True,
            "path": "index.html",
            "content": body,
            "start_line": 1,
            "end_line": 401,
            "total_lines": 401,
            "truncated": False,
        }
    )
    budgeter = ContextBudgeter(window_tokens=200_000, used_tokens=10_000, safety_fraction=0.8)
    blocks = [
        ToolResponse(id="1", tool_name="read_file", result=[Text(text=payload)]),
    ]
    trimmed, needs = budgeter.fit_content_blocks(blocks)
    assert not needs
    out = _text(trimmed[0])
    parsed = json.loads(out)
    assert parsed["content"] == body
    assert "omitted" not in out


def test_fit_still_sanitizes_run_command_blob_json_fields() -> None:
    """run_command is not on the sanitize skip list; denylisted blob keys still redact."""
    payload = json.dumps({"ok": True, "stdout": "x" * 2000, "data": "y" * 2000})
    budgeter = ContextBudgeter(window_tokens=200_000, used_tokens=10_000, safety_fraction=0.8)
    blocks = [
        ToolResponse(id="1", tool_name="run_command", result=[Text(text=payload)]),
    ]
    trimmed, _ = budgeter.fit_content_blocks(blocks)
    out = _text(trimmed[0])
    parsed = json.loads(out)
    assert parsed["stdout"] == "x" * 2000
    assert "omitted" in parsed["data"]
