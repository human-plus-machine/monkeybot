"""Async memory operations on :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from monkeybot.core.memory.index_format import INDEX_FILENAME, parse_index_entry_lines
from monkeybot.core.workspace.protocol import WorkspaceStorage


class MemoryPromotionError(RuntimeError):
    """Raised when promotion preconditions fail (path, run_id guard, missing file, etc.)."""


def _parse_index_lines(raw: str) -> list[str]:
    return parse_index_entry_lines(raw)


async def async_load_index(storage: WorkspaceStorage) -> list[str]:
    """Load ``INDEX.md`` lines, or ``[]`` if absent."""
    if not await storage.exists(INDEX_FILENAME):
        return []
    raw = await storage.read_text(INDEX_FILENAME)
    return _parse_index_lines(raw)


def _matches_query(line_lower: str, tokens: list[str]) -> bool:
    return all(tok in line_lower for tok in tokens)


def _memory_rel_skipped(rel_posix: str, skip_relative_prefixes: tuple[str, ...]) -> bool:
    if not skip_relative_prefixes:
        return False
    for raw_p in skip_relative_prefixes:
        p = raw_p.replace("\\", "/").strip("/")
        if not p:
            continue
        if rel_posix == p or rel_posix.startswith(p + "/"):
            return True
    return False


async def async_search_memory(query: str, storage: WorkspaceStorage, top_k: int = 5) -> list[str]:
    if top_k <= 0:
        return []
    q = query.strip()
    if not q:
        return []
    tokens = [t.lower() for t in q.split() if t]
    if not tokens:
        return []

    candidates = await async_load_index(storage)
    matches: list[str] = []
    for line in candidates:
        if _matches_query(line.lower(), tokens):
            matches.append(line)
            if len(matches) >= top_k:
                break
    return matches


async def async_search_memory_files(
    storage: WorkspaceStorage,
    query: str,
    *,
    max_hits: int = 40,
    skip_relative_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    q = query.lower().strip()
    if not q:
        return {"ok": True, "query": query, "hits": [], "note": "empty query"}

    all_paths = await storage.list_files("")
    if not all_paths:
        return {"ok": True, "query": query, "hits": [], "note": "empty memory tree"}

    suffixes = {".md", ".txt", ".markdown"}
    hits: list[dict[str, Any]] = []
    for rel in sorted(all_paths):
        if len(hits) >= max_hits:
            break
        low = rel.lower()
        if not any(low.endswith(s) for s in suffixes):
            continue
        rel_posix = rel.replace("\\", "/")
        if _memory_rel_skipped(rel_posix, skip_relative_prefixes):
            continue
        try:
            text = await storage.read_text(rel)
        except OSError:
            continue
        except FileNotFoundError:
            continue
        lower = text.lower()
        pos = lower.find(q)
        if pos < 0:
            continue
        start = max(0, pos - 60)
        end = min(len(text), pos + len(q) + 80)
        snippet = text[start:end].replace("\n", " ")
        hits.append({"path": rel, "snippet": snippet, "match_offset": pos})
    return {"ok": True, "query": query, "hits": hits, "truncated": len(hits) >= max_hits}


async def async_promote_to_memory(run_id: str, file: Path, storage: WorkspaceStorage) -> None:
    """Copy a local scratch file into ``semantic/`` on ``storage``, then delete the source.

    ``file`` must be a path on the process filesystem (e.g. agent workspace scratch), not a
    storage-relative key. The destination is always ``semantic/{filename}`` on ``storage``.
    A future storage-key source variant would be needed if the artifact already lives only
    in GCS/S3.
    """
    src = file.resolve()
    if not src.is_file():
        raise MemoryPromotionError("Source path must be an existing regular file.")

    if run_id not in src.parts:
        raise MemoryPromotionError(
            "Source path must include the run identifier as a path segment.",
        )

    dest_key = f"semantic/{src.name}"
    text = src.read_text(encoding="utf-8")
    await storage.write_text(dest_key, text)
    try:
        src.unlink()
    except OSError as exc:
        raise MemoryPromotionError(f"Failed to remove source after promote: {exc}") from exc


__all__ = [
    "INDEX_FILENAME",
    "MemoryPromotionError",
    "async_load_index",
    "async_search_memory_files",
    "async_promote_to_memory",
]
