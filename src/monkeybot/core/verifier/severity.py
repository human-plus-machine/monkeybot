"""Severity cap for the verifier escalation ladder."""

from __future__ import annotations

_ORDER = ("none", "nudge", "replan", "steer", "block")


def cap_severity(requested: str, maximum: str) -> str:
    """Return the weaker of ``requested`` and the configured ceiling."""
    want = requested if requested in _ORDER else "none"
    ceiling = maximum if maximum in _ORDER else "nudge"
    return want if _ORDER.index(want) <= _ORDER.index(ceiling) else ceiling
