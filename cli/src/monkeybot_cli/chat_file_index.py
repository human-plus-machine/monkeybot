"""Pure helpers for the ``@`` file-mention palette in ``monkeybot chat``."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_AT_TOKEN_RE = re.compile(r"@(\S*)$")

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "data",
    }
)


def detect_at_token(line: str, col: int) -> tuple[int, str] | None:
    """Return ``(start_col, query)`` if the cursor sits in an ``@token``.

    Only triggers when the ``@`` starts the line or follows whitespace, so
    mid-word ``@`` (e.g. ``a@b.com``) never matches.
    """
    prefix = line[:col]
    match = _AT_TOKEN_RE.search(prefix)
    if match is None:
        return None
    start = match.start()
    if start > 0 and not prefix[start - 1].isspace():
        return None
    return start, match.group(1)


def list_workspace_files(root: Path, *, limit: int = 3000) -> list[str]:
    """List files under ``root``, preferring git (honors .gitignore)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if result.returncode == 0:
            files = [line for line in result.stdout.splitlines() if line.strip()]
            if files:
                return sorted(files)[:limit]
    except (OSError, subprocess.SubprocessError):
        pass
    return _walk_files(root, limit=limit)


def _walk_files(root: Path, *, limit: int) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            out.append(rel.replace(os.sep, "/"))
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)


def fuzzy_filter_files(files: list[str], query: str, *, limit: int = 8) -> list[str]:
    """Case-insensitive subsequence match, ranked basename-prefix > path-prefix > contiguity."""
    if not query:
        return files[:limit]
    q = query.lower()
    scored: list[tuple[int, int, str]] = []
    for path in files:
        low = path.lower()
        basename = low.rsplit("/", 1)[-1]
        if not _is_subsequence(q, low):
            continue
        if basename.startswith(q):
            rank = 0
        elif low.startswith(q):
            rank = 1
        elif q in low:
            rank = 2
        else:
            rank = 3
        scored.append((rank, len(path), path))
    scored.sort()
    return [path for _, _, path in scored[:limit]]


def _is_subsequence(query: str, text: str) -> bool:
    it = iter(text)
    return all(ch in it for ch in query)
