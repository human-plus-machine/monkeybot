"""Process-local observability flags (no OpenTelemetry imports)."""

from __future__ import annotations

_initialized = False
_enabled = False


def is_observability_enabled() -> bool:
    return _enabled
