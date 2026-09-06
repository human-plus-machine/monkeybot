"""Typed constraint matching against tool names and arguments."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from monkeybot.core.persistence.goal_ledger import Constraint, ConstraintKind

_PATH_KEYS = ("path", "file_path", "file", "filename", "dest", "destination", "target")
_PATH_LIST_KEYS = ("paths", "files")
_COMMAND_KEYS = ("command", "cmd", "script", "argv")
WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
READ_TOOLS = frozenset({"read_file", "glob", "search"})


def path_args(args: dict[str, Any] | None) -> tuple[str, ...]:
    if not args:
        return ()
    found: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    for key in _PATH_LIST_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            found.extend(str(item).strip() for item in value if str(item).strip())
    return tuple(found)


def command_text(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    parts: list[str] = []
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
    return " ".join(parts)


def glob_match(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    pat = pattern.replace("\\", "/")
    if fnmatch(normalized, pat):
        return True
    if pat.endswith("/**"):
        prefix = pat[:-3]
        return normalized == prefix or normalized.startswith(prefix + "/")
    return False


def constraint_matches(
    constraint: Constraint,
    *,
    tool_name: str,
    args: dict[str, Any] | None,
) -> bool:
    if constraint.kind == ConstraintKind.FREE_TEXT:
        return False
    if constraint.kind == ConstraintKind.TOOL_NAME:
        return tool_name == constraint.pattern
    if constraint.kind == ConstraintKind.PATH_GLOB:
        return any(glob_match(path, constraint.pattern) for path in path_args(args))
    if constraint.kind == ConstraintKind.COMMAND_REGEX:
        import re

        text = command_text(args)
        if not text:
            return False
        try:
            return re.search(constraint.pattern, text) is not None
        except re.error:
            return False
    return False
