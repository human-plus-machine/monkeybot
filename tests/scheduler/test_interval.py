"""Tests for scheduler interval parsing."""

from __future__ import annotations

import pytest

from monkeybot.scheduler.interval import parse_interval_ms, parse_optional_duration_ms


def test_parse_interval_seconds() -> None:
    assert parse_interval_ms("20s") == 20_000
    assert parse_interval_ms("5m") == 300_000
    assert parse_interval_ms("1h") == 3_600_000
    assert parse_interval_ms(30) == 30_000


def test_parse_optional_duration() -> None:
    assert parse_optional_duration_ms(None) is None
    assert parse_optional_duration_ms("2h") == 7_200_000


def test_parse_interval_invalid() -> None:
    with pytest.raises(ValueError):
        parse_interval_ms("nope")
