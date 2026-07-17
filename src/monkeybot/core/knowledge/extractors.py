"""Extract indexable text from workspace / note files."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# Keep in sync with workspace grep ignores; also skip .monkeybot under workspace.
IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        ".monkeybot",
    }
)

_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".css",
        ".scss",
        ".html",
        ".htm",
        ".xml",
        ".svg",
        ".sh",
        ".bash",
        ".zsh",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".sql",
        ".graphql",
        ".vue",
        ".svelte",
    }
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_NULL_BYTE = b"\x00"

_SKIP_FILE_NAMES = frozenset(
    {
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
    }
)


def content_hash(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def is_probably_text(path: Path, *, max_file_bytes: int) -> bool:
    """True when the path looks like a text file under size limits."""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0 or size > max_file_bytes:
        return False
    suffix = path.suffix.lower()
    name = path.name.lower()
    if name == ".env.example" or suffix in _TEXT_SUFFIXES:
        return True
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    return _NULL_BYTE not in sample


def read_text_file(path: Path, *, max_file_bytes: int) -> str | None:
    """Read UTF-8 text (with replacement) or return None if unreadable / binary."""
    if not is_probably_text(path, max_file_bytes=max_file_bytes):
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > max_file_bytes or _NULL_BYTE in raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    if path.suffix.lower() in {".html", ".htm"}:
        text = strip_html(text)
    return text


def strip_html(html: str) -> str:
    """Lightweight HTML → text (tags stripped; entities left as-is)."""
    no_script = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _HTML_TAG_RE.sub(" ", no_script)


def walk_text_files(root: Path, *, max_file_bytes: int) -> list[Path]:
    """Walk ``root`` yielding text files, skipping ignore dirs."""
    if not root.is_dir():
        return []
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.startswith(".") and name != ".env.example":
                continue
            if name.lower() in _SKIP_FILE_NAMES:
                continue
            path = Path(dirpath) / name
            if is_probably_text(path, max_file_bytes=max_file_bytes):
                out.append(path)
    return out


__all__ = [
    "IGNORE_DIRS",
    "content_hash",
    "is_probably_text",
    "read_text_file",
    "strip_html",
    "walk_text_files",
]
