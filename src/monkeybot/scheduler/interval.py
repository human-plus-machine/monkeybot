"""Parse human interval strings for scheduled loops."""

from __future__ import annotations

import re

_INTERVAL_RE = re.compile(
    r"^\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?\s*$",
    re.IGNORECASE,
)

_UNIT_TO_MS: dict[str, int] = {
    "s": 1000,
    "sec": 1000,
    "secs": 1000,
    "second": 1000,
    "seconds": 1000,
    "m": 60_000,
    "min": 60_000,
    "mins": 60_000,
    "minute": 60_000,
    "minutes": 60_000,
    "h": 3_600_000,
    "hr": 3_600_000,
    "hrs": 3_600_000,
    "hour": 3_600_000,
    "hours": 3_600_000,
}


def parse_interval_ms(value: str | int | float) -> int:
    """Parse ``20s``, ``5m``, ``1h``, or raw seconds into milliseconds."""
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds <= 0:
            raise ValueError("interval must be positive")
        return max(1, int(seconds * 1000))
    raw = str(value).strip()
    if not raw:
        raise ValueError("interval is required")
    match = _INTERVAL_RE.match(raw)
    if not match:
        raise ValueError(f"invalid interval: {value!r}")
    num = float(match.group("num"))
    if num <= 0:
        raise ValueError("interval must be positive")
    unit = (match.group("unit") or "s").lower()
    ms = int(num * _UNIT_TO_MS[unit])
    return max(1, ms)


def parse_optional_duration_ms(value: str | int | float | None) -> int | None:
    """Parse optional max-runtime style durations."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return parse_interval_ms(value)
