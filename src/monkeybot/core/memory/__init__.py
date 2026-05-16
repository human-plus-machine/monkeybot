"""File-backed memory index load, search, and promotion helpers."""

from __future__ import annotations

import asyncio
import re
import shutil
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

INDEX_FILENAME = "INDEX.md"

# Matches  [[folder/filename.md]]  inside an INDEX.md entry line.
_INDEX_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class MemoryPromotionError(RuntimeError):
    """Raised when promotion preconditions fail (path, run_id guard, missing file, etc.)."""


def _parse_index_lines(raw: str) -> list[str]:
    """Split index text into non-empty stripped lines."""
    return [stripped for line in raw.splitlines() if (stripped := line.strip())]


async def load_index(memory_path: Path) -> list[str]:
    """Load MEMORY_PATH/INDEX.md lines, or ``[]`` if memory root/index is absent.

    Raises:
        UnicodeDecodeError: If INDEX.md exists but is not valid UTF-8.
    """
    resolved = memory_path.resolve()
    if not await asyncio.to_thread(resolved.exists):
        return []
    index_file = resolved / INDEX_FILENAME
    if not await asyncio.to_thread(index_file.exists):
        return []

    raw = await asyncio.to_thread(index_file.read_text, encoding="utf-8")
    return _parse_index_lines(raw)


def _matches_query(line_lower: str, tokens: list[str]) -> bool:
    return all(tok in line_lower for tok in tokens)


def _memory_rel_skipped(rel_posix: str, skip_relative_prefixes: tuple[str, ...]) -> bool:
    """True when ``rel_posix`` is exactly a skipped prefix or lives under it."""
    if not skip_relative_prefixes:
        return False
    for raw_p in skip_relative_prefixes:
        p = raw_p.replace("\\", "/").strip("/")
        if not p:
            continue
        if rel_posix == p or rel_posix.startswith(p + "/"):
            return True
    return False


async def search_memory(query: str, memory_path: Path, top_k: int = 5) -> list[str]:
    """Return up to ``top_k`` index lines matching all whitespace-separated tokens (case-insensitive)."""
    if top_k <= 0:
        return []
    q = query.strip()
    if not q:
        return []
    tokens = [t.lower() for t in q.split() if t]
    if not tokens:
        return []

    candidates = await load_index(memory_path)
    matches: list[str] = []
    for line in candidates:
        if _matches_query(line.lower(), tokens):
            matches.append(line)
            if len(matches) >= top_k:
                break
    return matches


def search_memory_files(
    memory_root: Path,
    query: str,
    *,
    max_hits: int = 40,
    skip_relative_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Scan memory tree for UTF-8 text files containing ``query`` (case-insensitive substring).

    Used by the ``search_memory`` tool and by context curation. Runs synchronously; call
    from ``asyncio.to_thread`` in async code.

    ``skip_relative_prefixes`` drops hits whose path relative to ``memory_root`` is that
    directory or a file under it (POSIX-style, case-sensitive). Hooks pass ``("raw",)``
    so ``PRE_TOOL`` / curation do not surface ``memory/raw/`` post-tool telemetry as
    pseudo-memory; the ``search_memory`` tool omits this argument so operators can still
    search the full tree.
    """
    q = query.lower().strip()
    if not q:
        return {"ok": True, "query": query, "hits": [], "note": "empty query"}
    if not memory_root.exists():
        return {"ok": True, "query": query, "hits": [], "note": f"missing directory: {memory_root}"}

    hits: list[dict[str, Any]] = []
    suffixes = {".md", ".txt", ".markdown"}
    for path in sorted(memory_root.rglob("*")):
        if len(hits) >= max_hits:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        pos = lower.find(q)
        if pos < 0:
            continue
        try:
            rel = str(path.relative_to(memory_root))
        except ValueError:
            rel = path.name
        rel_posix = rel.replace("\\", "/")
        if _memory_rel_skipped(rel_posix, skip_relative_prefixes):
            continue
        start = max(0, pos - 60)
        end = min(len(text), pos + len(q) + 80)
        snippet = text[start:end].replace("\n", " ")
        hits.append({"path": rel, "snippet": snippet, "match_offset": pos})
    return {"ok": True, "query": query, "hits": hits, "truncated": len(hits) >= max_hits}


def _promote_move(src: Path, dest: Path) -> None:
    """Atomic replace when possible; fall back to ``shutil.move`` across devices."""
    try:
        src.replace(dest)
    except OSError:
        shutil.move(str(src), str(dest))


async def promote_to_memory(run_id: str, file: Path, memory_path: Path) -> None:
    """Move ``file`` into ``memory_path/semantic/{name}``, enforcing ``run_id`` path guard."""
    src = file.resolve()
    if not src.is_file():
        raise MemoryPromotionError("Source path must be an existing regular file.")

    if run_id not in src.parts:
        raise MemoryPromotionError(
            "Source path must include the run identifier as a path segment.",
        )

    semantic = (memory_path / "semantic").resolve()
    dest = (semantic / src.name).resolve()

    await asyncio.to_thread(semantic.mkdir, parents=True, exist_ok=True)

    await asyncio.to_thread(_promote_move, src, dest)
