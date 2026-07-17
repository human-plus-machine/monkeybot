"""Chunk text for FTS indexing (~token-sized windows with overlap)."""

from __future__ import annotations

import re

from monkeybot.core.knowledge.types import SourceType, TextChunk

# Rough heuristic: ~4 characters per token for mixed code/prose.
_CHARS_PER_TOKEN = 4
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def chunk_text(
    text: str,
    *,
    path: str,
    source_type: SourceType,
    chunk_tokens: int = 700,
    overlap_ratio: float = 0.12,
) -> list[TextChunk]:
    """Split ``text`` into overlapping line-aligned chunks with a path/heading prefix."""
    if not text or not text.strip():
        return []

    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    target_chars = max(200, int(chunk_tokens * _CHARS_PER_TOKEN))
    overlap_chars = max(0, int(target_chars * max(0.0, min(0.5, overlap_ratio))))

    chunks: list[TextChunk] = []
    start_idx = 0
    n = len(lines)

    while start_idx < n:
        char_count = 0
        end_idx = start_idx
        while end_idx < n and (char_count < target_chars or end_idx == start_idx):
            char_count += len(lines[end_idx])
            end_idx += 1

        body = "".join(lines[start_idx:end_idx])
        start_line = start_idx + 1
        end_line = end_idx
        heading = _nearest_heading(lines, start_idx)
        prefix_parts = [path]
        if heading:
            prefix_parts.append(heading)
        prefixed = f"{' · '.join(prefix_parts)}\n{body}"
        chunks.append(
            TextChunk(
                path=path,
                source_type=source_type,
                start_line=start_line,
                end_line=end_line,
                text=prefixed,
            )
        )

        if end_idx >= n:
            break

        # Advance with overlap: walk back until overlap_chars of content remain.
        if overlap_chars <= 0:
            start_idx = end_idx
            continue
        back_chars = 0
        new_start = end_idx
        while new_start > start_idx and back_chars < overlap_chars:
            new_start -= 1
            back_chars += len(lines[new_start])
        # Ensure forward progress
        start_idx = max(start_idx + 1, new_start)

    return chunks


def _nearest_heading(lines: list[str], start_idx: int) -> str | None:
    for i in range(start_idx, -1, -1):
        m = _HEADING_RE.match(lines[i].rstrip("\n"))
        if m:
            return m.group(2).strip()
    return None


__all__ = ["chunk_text", "estimate_tokens"]
