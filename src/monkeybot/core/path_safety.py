"""Helpers for safe filesystem paths from user-supplied identifiers."""

from __future__ import annotations

import re
from typing import Final

GLOB_METACHARACTERS: Final[str] = "*?[]"

_INVALID_PATH_COMPONENT_RE = re.compile(r"[\\/]|\.{2,}|[*?\[\]]")
_RESERVED_PATH_COMPONENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def sanitize_path_component(name: str) -> str:
    """Sanitize a string for use as a single path component (no directory separators)."""
    sanitized = _INVALID_PATH_COMPONENT_RE.sub("_", name)
    if sanitized in _RESERVED_PATH_COMPONENTS:
        return "_"
    return sanitized


def validate_session_id_component(value: str) -> str:
    """Reject client session ids that are unsafe as a single path component."""
    if value in _RESERVED_PATH_COMPONENTS:
        raise ValueError(
            "session_id must be 1-128 alphanumeric chars, dots, underscores, colons, or hyphens"
        )
    if not SESSION_ID_RE.match(value):
        raise ValueError(
            "session_id must be 1-128 alphanumeric chars, dots, underscores, colons, or hyphens"
        )
    return value


__all__ = [
    "GLOB_METACHARACTERS",
    "SESSION_ID_RE",
    "sanitize_path_component",
    "validate_session_id_component",
]
