"""Helpers for safe filesystem paths from user-supplied identifiers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

GLOB_METACHARACTERS: Final[str] = "*?[]"
_GLOB_CHAR_CLASS = "".join("\\" + ch if ch in r"[]\\" else ch for ch in GLOB_METACHARACTERS)
_INVALID_PATH_COMPONENT_RE = re.compile(rf"[\\/]|\.{{2,}}|[{_GLOB_CHAR_CLASS}]")
_RESERVED_PATH_COMPONENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def sanitize_path_component(name: str) -> str:
    """Sanitize a string for use as a single path component (no directory separators)."""
    sanitized = _INVALID_PATH_COMPONENT_RE.sub("_", name)
    if sanitized in _RESERVED_PATH_COMPONENTS:
        return "_"
    return sanitized


def is_legacy_path_component_safe(name: str) -> bool:
    """True when ``name`` is safe as a literal pre-sanitization directory name.

    Allows glob metacharacters (those ids used to land on disk as-is) but rejects
    separators, ``..`` runs, and reserved single-component names.
    """
    if name in _RESERVED_PATH_COMPONENTS:
        return False
    without_globs = name.translate(str.maketrans("", "", GLOB_METACHARACTERS))
    return not _INVALID_PATH_COMPONENT_RE.search(without_globs)


def resolve_legacy_or_sanitized_dir(parent: Path, name: str) -> Path:
    """Prefer the sanitized child dir; reuse a safe legacy folder when it exists."""
    safe = sanitize_path_component(name)
    sanitized = parent / safe
    if safe == name:
        return sanitized
    if is_legacy_path_component_safe(name):
        legacy = parent / name
        if legacy.exists() and not sanitized.exists():
            return legacy
    return sanitized


def validate_session_id_component(value: str) -> str:
    """Reject client session ids that are unsafe as a single path component."""
    if value in _RESERVED_PATH_COMPONENTS or not SESSION_ID_RE.match(value):
        raise ValueError(
            "session_id must be 1-128 alphanumeric chars, dots, underscores, colons, or hyphens"
        )
    return value


__all__ = [
    "GLOB_METACHARACTERS",
    "SESSION_ID_RE",
    "is_legacy_path_component_safe",
    "resolve_legacy_or_sanitized_dir",
    "sanitize_path_component",
    "validate_session_id_component",
]
