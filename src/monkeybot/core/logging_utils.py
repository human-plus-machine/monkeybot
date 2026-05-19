"""Shared logging helpers for gateway, workers, and deploy entrypoints."""

from __future__ import annotations

import logging


def normalize_log_level(raw: str | None, *, default: str = "INFO") -> int:
    """Map ``LOG_LEVEL`` env strings to :mod:`logging` level ints (case-insensitive)."""
    name = (raw or default).strip().upper()
    level = logging.getLevelName(name)
    if isinstance(level, int):
        return level
    raise ValueError(f"invalid log level: {raw!r}")
