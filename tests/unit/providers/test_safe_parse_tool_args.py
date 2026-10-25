"""Provider stream argument parsing hardening."""

from __future__ import annotations

import logging

import pytest

from monkeybot.providers._utils import safe_parse_tool_args


def test_safe_parse_tool_args_valid_object() -> None:
    parsed, parse_error = safe_parse_tool_args(
        '{"x": 1}',
        call_id="c1",
        tool_name="echo",
        provider="bedrock",
    )
    assert parsed == {"x": 1}
    assert parse_error is None


def test_safe_parse_tool_args_empty_string() -> None:
    parsed, parse_error = safe_parse_tool_args(
        "",
        call_id="c1",
        tool_name="echo",
        provider="openai_compat",
    )
    assert parsed == {}
    assert parse_error is None


def test_safe_parse_tool_args_logs_on_decode_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="monkeybot.providers._utils"):
        parsed, parse_error = safe_parse_tool_args(
            "not-json",
            call_id="c9",
            tool_name="run_command",
            provider="vertex_claude",
        )
    assert parsed == {}
    assert parse_error is not None
    assert "malformed tool args JSON" in parse_error
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "stream_parse_repair" in joined
    assert "c9" in joined


def test_safe_parse_tool_args_non_object_args() -> None:
    parsed, parse_error = safe_parse_tool_args(
        '["a"]',
        call_id="c2",
        tool_name="echo",
        provider="claude",
    )
    assert parsed == {}
    assert parse_error is not None
    assert "JSON object" in parse_error
