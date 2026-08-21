"""Shared constants for gateway pending-response buses."""

from __future__ import annotations

# Bounded ring of pending keys that have been resolved / abandoned / Stop-cancelled.
TERMINATED_PENDING_KEYS_MAXLEN: int = 256

__all__ = ["TERMINATED_PENDING_KEYS_MAXLEN"]
