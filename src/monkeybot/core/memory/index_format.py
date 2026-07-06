"""INDEX.md line format: append-only entries, sliding cap, and archive helpers."""

from __future__ import annotations

import os

INDEX_FILENAME = "INDEX.md"
INDEX_ARCHIVE_FILENAME = "INDEX.archive.md"
DEFAULT_INDEX_HEADER = "# Memory Index"


def index_cap_from_env() -> int:
    """Max INDEX.md entries kept before older ones move to the archive file."""
    raw = os.getenv("MEMORY_INDEX_CAP", "200").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 200


def is_index_entry_line(line: str) -> bool:
    """True for canonical index rows: ``- [[folder/file.md]] | tags: ... | summary``."""
    stripped = line.strip()
    return stripped.startswith("- [[") and "]]" in stripped


def is_legacy_index_line(line: str) -> bool:
    """Non-header lines from pre-recency INDEX files (plain bullets or free text)."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return False
    return True


def parse_index_entry_lines(raw: str) -> list[str]:
    """Return index entry lines in file order (newest last when append-only)."""
    entries: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if is_index_entry_line(stripped) or is_legacy_index_line(stripped):
            entries.append(stripped)
    return entries


def split_index_document(raw: str) -> tuple[str, list[str]]:
    """Split INDEX.md into header preamble and ordered entry lines."""
    entries = parse_index_entry_lines(raw)
    if not entries:
        return (raw.strip() or DEFAULT_INDEX_HEADER), []
    first = entries[0]
    pos = raw.find(first)
    if pos <= 0:
        return DEFAULT_INDEX_HEADER, entries
    header = raw[:pos].strip() or DEFAULT_INDEX_HEADER
    return header, entries


def format_index_document(header: str, entry_lines: list[str]) -> str:
    """Serialize header + append-ordered entry lines."""
    hdr = (header or DEFAULT_INDEX_HEADER).strip()
    if not entry_lines:
        return hdr + "\n"
    return hdr + "\n\n" + "\n".join(entry_lines) + "\n"


def append_index_entries(existing_raw: str, new_entries: list[str]) -> str:
    """Append new entries to the end (recency order: oldest first, newest last)."""
    header, entries = split_index_document(existing_raw)
    for entry in new_entries:
        line = entry.strip()
        if line and line not in entries:
            entries.append(line)
    return format_index_document(header, entries)


def apply_index_entry_cap(
    entry_lines: list[str],
    cap: int,
) -> tuple[list[str], list[str]]:
    """Return ``(kept_recent, archived_older)`` keeping the last *cap* entries."""
    if cap <= 0 or len(entry_lines) <= cap:
        return list(entry_lines), []
    archived = entry_lines[: len(entry_lines) - cap]
    kept = entry_lines[-cap:]
    return kept, archived


def merge_archive_content(existing_archive: str, overflow: list[str]) -> str:
    """Prepend newly archived lines (older) before existing archive content."""
    if not overflow:
        return existing_archive
    new_block = "\n".join(overflow)
    if not existing_archive.strip():
        return new_block + "\n"
    return new_block + "\n" + existing_archive.rstrip() + "\n"


def memory_window_slice(entry_lines: list[str], window: int) -> list[str]:
    """Return the most recent *window* index entries."""
    if window <= 0 or len(entry_lines) <= window:
        return list(entry_lines)
    return entry_lines[-window:]
