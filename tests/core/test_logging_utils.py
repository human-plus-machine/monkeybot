"""Tests for :mod:`monkeybot.core.logging_utils`."""

from __future__ import annotations

import logging

import pytest

from monkeybot.core.logging_utils import kv, normalize_log_level


def test_normalize_log_level_case_insensitive() -> None:
    assert normalize_log_level("info") == logging.INFO
    assert normalize_log_level("INFO") == logging.INFO
    assert normalize_log_level("warning") == logging.WARNING


def test_normalize_log_level_default() -> None:
    assert normalize_log_level(None, default="DEBUG") == logging.DEBUG


def test_normalize_log_level_invalid() -> None:
    with pytest.raises(ValueError, match="invalid log level"):
        normalize_log_level("not-a-level")


def test_kv_formats_fields() -> None:
    assert kv(request_id="r1", thread_id="t1", turn=2) == "request_id=r1 thread_id=t1 turn=2"


def test_kv_skips_none() -> None:
    assert kv(request_id="r1", thread_id=None) == "request_id=r1"
