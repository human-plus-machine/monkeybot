"""Helpers for safe filesystem paths from user-supplied identifiers."""

from __future__ import annotations


def sanitize_path_component(name: str) -> str:
    """Sanitize a string for use as a single path component (no directory separators)."""
    return name.replace("/", "_").replace("..", "_")


__all__ = ["sanitize_path_component"]
