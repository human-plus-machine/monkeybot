"""Async memory operations on :class:`~monkeybot.core.workspace.protocol.WorkspaceStorage`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from monkeybot.core.memory.index_format import INDEX_FILENAME, parse_index_entry_lines
from monkeybot.core.workspace.protocol import WorkspaceStorage

# Soft cap so a pathological note cannot dominate the tool result.
_MAX_BODY_CHARS = 2000
# Full note bodies only for the top-N keyword hits (path= fetches always include body).
_MAX_FULL_BODY_HITS = 3

# Shared stopwords for memory search + organizer link-candidate queries.
MEMORY_SEARCH_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "were",
        "was",
        "are",
        "is",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "as",
        "it",
        "its",
        "or",
        "be",
        "been",
        "into",
        "via",
        "about",
        "what",
        "when",
        "where",
        "which",
        "how",
        "any",
        "like",
        "using",
        "used",
        "agent",
        "successfully",
        "failed",
        "error",
        "command",
        "tool",
        "file",
        "python",
        "script",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]{2,}")


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


def _query_tokens(query: str) -> list[str]:
    """Distinctive tokens from a search query (order preserved, stopwords dropped)."""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(query or ""):
        tok = raw.lower().strip(".-_/")
        if len(tok) < 2 or tok in MEMORY_SEARCH_STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        tokens.append(tok)
    return tokens


def _min_token_hits(n_tokens: int) -> int:
    """Short queries AND; long kitchen-sink queries need a fraction of tokens."""
    if n_tokens <= 0:
        return 1
    if n_tokens <= 3:
        return n_tokens
    return max(3, (n_tokens + 2) // 3)


def _score_memory_haystack(
    lower: str, *, phrase: str, tokens: list[str]
) -> tuple[int, int]:
    """Return (score, match_offset). score 0 means no hit."""
    if not lower.strip():
        return 0, -1

    phrase_pos = lower.find(phrase) if phrase else -1
    if not tokens:
        if phrase_pos < 0:
            return 0, -1
        return 1, phrase_pos

    hit_tokens = [t for t in tokens if t in lower]
    token_hits = len(hit_tokens)
    if token_hits < _min_token_hits(len(tokens)) and phrase_pos < 0:
        return 0, -1

    score = token_hits
    if phrase_pos >= 0:
        score += max(2, len(tokens) // 2)  # prefer exact phrase

    if phrase_pos >= 0:
        offset = phrase_pos
    elif hit_tokens:
        offset = min(lower.find(t) for t in hit_tokens)
    else:
        offset = -1
    return score, offset


def _links_from_note(text: str, *, supersedes: str | None) -> list[dict[str, str]]:
    from monkeybot.core.memory.note_format import extract_memory_wiki_links

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    if supersedes:
        path = supersedes.replace("\\", "/").lstrip("./")
        links.append({"path": path, "kind": "supersedes"})
        seen.add(path)
    for path in extract_memory_wiki_links(text):
        if path in seen:
            continue
        seen.add(path)
        links.append({"path": path, "kind": "related"})
    return links


def _snippet_around(haystack: str, pos: int, query_len: int) -> str:
    if pos < 0:
        return haystack[:140].replace("\n", " ")
    start = max(0, pos - 60)
    end = min(len(haystack), pos + max(query_len, 1) + 80)
    return haystack[start:end].replace("\n", " ")


def memory_hit_from_text(
    rel: str,
    text: str,
    *,
    match_offset: int,
    via: str | None = None,
    include_body: bool = True,
    query_len: int = 0,
    score: int | None = None,
) -> dict[str, Any]:
    """Build a search_memory hit from note file text."""
    from monkeybot.core.memory.note_format import parse_memory_note

    meta, body = parse_memory_note(text)
    haystack = body if meta is not None else text
    snippet = _snippet_around(haystack, match_offset, query_len)
    hit: dict[str, Any] = {
        "path": rel.replace("\\", "/"),
        "snippet": snippet,
        "match_offset": match_offset,
        "links": _links_from_note(
            text, supersedes=meta.supersedes if meta is not None else None
        ),
    }
    if score is not None:
        hit["score"] = score
    if include_body:
        body_text = haystack.rstrip()
        if len(body_text) > _MAX_BODY_CHARS:
            hit["body"] = body_text[:_MAX_BODY_CHARS]
            hit["body_truncated"] = True
        else:
            hit["body"] = body_text
            hit["body_truncated"] = False
    if via:
        hit["via"] = via
    if meta is not None:
        hit["type"] = meta.type
        hit["status"] = meta.status
    return hit


async def async_load_memory_hit(
    storage: WorkspaceStorage,
    path: str,
    *,
    include_retired: bool = False,
    include_body: bool = True,
    via: str | None = None,
) -> dict[str, Any] | None:
    """Load one memory note as a hit, or None if missing/retired."""
    from monkeybot.core.memory.note_format import parse_memory_note

    rel = path.replace("\\", "/").lstrip("./")
    try:
        text = await storage.read_text(rel)
    except (OSError, FileNotFoundError):
        return None
    meta, _ = parse_memory_note(text)
    if meta is not None and meta.status != "active" and not include_retired:
        return None
    return memory_hit_from_text(
        rel, text, match_offset=0, include_body=include_body, via=via
    )


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
    folder: str | None = None,
    include_retired: bool = False,
) -> dict[str, Any]:
    from monkeybot.core.memory.note_format import (
        TYPED_FOLDERS,
        folder_from_rel_path,
        parse_memory_note,
    )

    q = query.lower().strip()
    if not q:
        return {"ok": True, "query": query, "hits": [], "note": "empty query"}

    tokens = _query_tokens(q)
    all_paths = await storage.list_files("")
    if not all_paths:
        return {"ok": True, "query": query, "hits": [], "note": "empty memory tree"}

    folder_norm = (folder or "").strip().lower() or None
    if folder_norm and folder_norm not in TYPED_FOLDERS:
        return {
            "ok": True,
            "query": query,
            "hits": [],
            "note": f"unknown folder filter: {folder_norm}",
        }

    suffixes = {".md", ".txt", ".markdown"}
    scored: list[tuple[int, dict[str, Any]]] = []
    for rel in sorted(all_paths):
        low = rel.lower()
        if not any(low.endswith(s) for s in suffixes):
            continue
        rel_posix = rel.replace("\\", "/")
        if _memory_rel_skipped(rel_posix, skip_relative_prefixes):
            continue
        top = folder_from_rel_path(rel_posix)
        if folder_norm and top != folder_norm:
            continue
        if top is None and rel_posix in ("INDEX.md", "chat_log.md"):
            continue
        try:
            text = await storage.read_text(rel)
        except OSError:
            continue
        except FileNotFoundError:
            continue
        meta, body = parse_memory_note(text)
        if meta is not None and meta.status != "active" and not include_retired:
            continue
        # Prefer matching body; fall back to full text for status-less notes.
        haystack = body if meta is not None else text
        lower = haystack.lower()
        score, pos = _score_memory_haystack(lower, phrase=q, tokens=tokens)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                memory_hit_from_text(
                    rel,
                    text,
                    match_offset=pos,
                    include_body=True,
                    query_len=len(q),
                    score=score,
                ),
            )
        )

    # Higher score first; stable path order for ties.
    scored.sort(key=lambda item: (-item[0], item[1].get("path") or ""))
    hits = [h for _, h in scored[:max_hits]]
    for i, hit in enumerate(hits):
        if i >= _MAX_FULL_BODY_HITS:
            hit.pop("body", None)
            hit.pop("body_truncated", None)
    return {"ok": True, "query": query, "hits": hits, "truncated": len(scored) >= max_hits}


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
    from monkeybot.core.memory.note_format import format_memory_note

    note = format_memory_note(note_type="semantic", status="active", body=text)
    await storage.write_text(dest_key, note)
    try:
        src.unlink()
    except OSError as exc:
        raise MemoryPromotionError(f"Failed to remove source after promote: {exc}") from exc


__all__ = [
    "INDEX_FILENAME",
    "MEMORY_SEARCH_STOPWORDS",
    "MemoryPromotionError",
    "async_load_index",
    "async_load_memory_hit",
    "async_search_memory_files",
    "async_promote_to_memory",
    "memory_hit_from_text",
]
