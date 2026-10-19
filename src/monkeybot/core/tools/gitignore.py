"""Minimal ``.gitignore`` matching for the Python grep walker.

Mirrors what ripgrep applies with ``--no-require-git --no-ignore-global
--no-ignore-parent --no-ignore-dot --no-ignore-exclude``: only ``.gitignore``
files inside the searched tree, each scoped to its own directory, last match
wins, ``!`` re-includes. Not a full git implementation (no ``core.excludesFile``,
no ``.git/info/exclude``) — that is the point: the rg and Python engines must
agree, and neither reads per-user state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**", i):
                i += 2
                if pattern.startswith("/", i):
                    out.append("(?:.*/)?")  # ``**/`` — zero or more leading directories
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end
        elif ch == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _parse_line(raw: str) -> _Rule | None:
    line = raw.rstrip("\n\r")
    stripped = line.rstrip(" ") if not line.endswith("\\ ") else line
    if not stripped or stripped.startswith("#"):
        return None
    negate = stripped.startswith("!")
    if negate:
        stripped = stripped[1:]
    if stripped.startswith(("\\#", "\\!")):
        stripped = stripped[1:]
    dir_only = stripped.endswith("/")
    stripped = stripped.rstrip("/")
    if not stripped:
        return None
    anchored = "/" in stripped
    stripped = stripped.lstrip("/")
    body = _glob_to_regex(stripped)
    prefix = "" if anchored else "(?:.*/)?"
    try:
        regex = re.compile(f"^{prefix}{body}$")
    except re.error:
        return None
    return _Rule(regex=regex, negate=negate, dir_only=dir_only)


class GitignoreRules:
    """Ordered ``.gitignore`` rules in effect at one directory (root first)."""

    def __init__(self, scoped: tuple[tuple[str, tuple[_Rule, ...]], ...] = ()) -> None:
        # Each entry: (directory of the .gitignore relative to the walk anchor, rules).
        self._scoped = scoped

    def with_directory(self, directory: Path, rel_dir: str) -> GitignoreRules:
        """Rules including ``directory/.gitignore`` (if any); ``rel_dir`` is its anchor-relative path."""
        try:
            text = (directory / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self
        rules = tuple(rule for raw in text.splitlines() if (rule := _parse_line(raw)) is not None)
        if not rules:
            return self
        return GitignoreRules((*self._scoped, (rel_dir, rules)))

    def is_ignored(self, rel_path: str, *, is_dir: bool) -> bool:
        """``rel_path`` is relative to the walk anchor, posix-separated."""
        ignored = False
        for scope, rules in self._scoped:
            if scope:
                if not rel_path.startswith(scope + "/"):
                    continue
                local = rel_path[len(scope) + 1 :]
            else:
                local = rel_path
            for rule in rules:
                if rule.dir_only and not is_dir:
                    continue
                if rule.regex.match(local):
                    ignored = not rule.negate
        return ignored
