"""Tests for span attribute truncation."""

from __future__ import annotations

from monkeybot.observability.spans import truncate


def test_truncate_empty() -> None:
    assert truncate("") == ""


def test_truncate_at_limit_ascii() -> None:
    value = "a" * 8192
    assert truncate(value, max_bytes=8192) == value


def test_truncate_over_limit_ascii() -> None:
    value = "a" * 8300
    out = truncate(value, max_bytes=8192)
    assert out.endswith("…[truncated]")
    assert len(out.encode("utf-8")) <= 8192 + 32


def test_truncate_multibyte_boundary() -> None:
    value = "é" * 5000
    out = truncate(value, max_bytes=100)
    out.encode("utf-8")
