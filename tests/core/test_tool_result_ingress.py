"""Tests for tool result ingress sanitization and caps."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from monkeybot.core.context.tool_result_ingress import (
    cap_tool_result_text,
    format_mcp_content_block,
    sanitize_tool_result_text,
    summarize_tool_result_text,
)
from monkeybot.core.context.tool_output_policy import (
    register_mcp_tool_names,
    resolve_tool_budget,
    reset_tool_output_policy_cache_for_tests,
)
from monkeybot.core.mcp.mcp_client import _normalize_call_tool_result


def test_sanitize_redacts_data_url() -> None:
    blob = "A" * 1200
    text = f"prefix data:image/png;base64,{blob} suffix"
    out = sanitize_tool_result_text(text)
    assert blob not in out
    assert "omitted" in out


def test_sanitize_redacts_json_data_field() -> None:
    payload = {"path": "/tmp/x.png", "data": "x" * 5000}
    out = sanitize_tool_result_text(json.dumps(payload))
    parsed = json.loads(out)
    assert "omitted" in parsed["data"]
    assert parsed["path"] == "/tmp/x.png"


def test_sanitize_redacts_short_base64_in_json_data_field() -> None:
    payload = {"data": "SGVsbG8gV29ybGQh"}
    out = sanitize_tool_result_text(json.dumps(payload))
    parsed = json.loads(out)
    assert "omitted" in parsed["data"]


def test_sanitize_keeps_short_plain_text_in_json_data_field() -> None:
    payload = {"data": "not base64 text"}
    out = sanitize_tool_result_text(json.dumps(payload))
    parsed = json.loads(out)
    assert parsed["data"] == "not base64 text"


def test_sanitize_redacts_long_bare_base64_run() -> None:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    blob = (alphabet * 20)[:1200]
    out = sanitize_tool_result_text(blob)
    assert blob not in out
    assert "base64 run" in out


def test_sanitize_keeps_repeated_plain_text() -> None:
    text = "x" * 40_000
    assert sanitize_tool_result_text(text) == text


def test_cap_tool_result_text_truncates() -> None:
    text = "line\n" * 20_000
    out = cap_tool_result_text(text, max_chars=1000)
    assert len(out) > 1000
    assert "truncated" in out
    assert len(out) < len(text)


def test_summarize_tool_result_text_caps_and_sanitizes() -> None:
    blob = "B" * 20_000
    text = json.dumps({"data": blob})
    out = summarize_tool_result_text(text, max_chars=500)
    assert blob not in out
    assert len(out) <= 600


def test_format_mcp_content_block_image() -> None:
    block = SimpleNamespace(type="image", data="Z" * 5000, mimeType="image/png")
    out = format_mcp_content_block(block)
    assert "image omitted" in out
    assert "Z" * 100 not in out


def test_format_mcp_content_block_audio() -> None:
    block = SimpleNamespace(type="audio", data="Z" * 3000, mimeType="audio/wav")
    out = format_mcp_content_block(block)
    assert "audio omitted" in out


def test_normalize_call_tool_result_uses_ingress() -> None:
    image_block = SimpleNamespace(type="image", data="X" * 4000, mimeType="image/png")
    result = SimpleNamespace(content=[image_block])
    out = _normalize_call_tool_result(result)
    assert "image omitted" in out
    assert "X" * 100 not in out


def test_resolve_tool_budget_mcp_default() -> None:
    reset_tool_output_policy_cache_for_tests()
    register_mcp_tool_names(["browser__browser_screenshot"])
    budget = resolve_tool_budget("browser__browser_screenshot")
    assert budget is not None
    assert budget.max_output_lines == 120


def test_resolve_tool_budget_ignores_unregistered_double_underscore_name() -> None:
    reset_tool_output_policy_cache_for_tests()
    assert resolve_tool_budget("my__custom_tool") is None


def test_resolve_tool_budget_yaml_wildcard(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_tool_output_policy_cache_for_tests()
    cfg = tmp_path / "allowlist.yaml"
    cfg.write_text(
        "tool_output:\n  '*':\n    max_output_lines: 77\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", str(cfg))
    reset_tool_output_policy_cache_for_tests()
    register_mcp_tool_names(["browser__browser_goto"])
    budget = resolve_tool_budget("browser__browser_goto")
    assert budget is not None
    assert budget.max_output_lines == 77
    reset_tool_output_policy_cache_for_tests()
