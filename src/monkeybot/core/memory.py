"""File-backed memory index load, search, and promotion helpers."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

INDEX_FILENAME = "INDEX.md"


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
