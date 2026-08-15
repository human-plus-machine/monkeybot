"""Helpers for safe filesystem paths from user-supplied identifiers."""

from __future__ import annotations

from typing import Final

GLOB_METACHARACTERS: Final[str] = "*?[]"


def sanitize_path_component(name: str) -> str:
    """Sanitize a string for use as a single path component (no directory separators)."""
    sanitized = (
        name.replace("\\", "_")
        .replace("/", "_")
        .replace("..", "_")
    )
    for ch in GLOB_METACHARACTERS:
        sanitized = sanitized.replace(ch, "_")
    return sanitized


__all__ = ["GLOB_METACHARACTERS", "sanitize_path_component"]
