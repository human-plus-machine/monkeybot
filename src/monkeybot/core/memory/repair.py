"""Detect unreadable memory files, quarantine them, and restore a usable INDEX.md."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from monkeybot.core.memory.index_format import (
    DEFAULT_INDEX_HEADER,
    INDEX_FILENAME,
    format_index_document,
    format_index_entry_line,
    is_index_entry_line,
    split_index_document,
    wiki_target_from_line,
)
from monkeybot.core.memory.note_format import TYPED_FOLDERS, parse_memory_note
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)

_QUARANTINE_ROOT = ".quarantine"
_TYPED_PREFIXES = tuple(f"{f}/" for f in TYPED_FOLDERS)


@dataclass
class RepairReport:
    """Outcome of :func:`repair_memory_tree`."""

    quarantined: list[str] = field(default_factory=list)
    index_rebuilt: bool = False
    index_pruned: list[str] = field(default_factory=list)
    entries_written: int = 0


def _summary_from_body(body: str) -> str:
    flat = " ".join((body or "").strip().split())
    if not flat:
        return "(empty note)"
    return flat


async def _quarantine(
    storage: WorkspaceStorage,
    rel: str,
    quarantine_prefix: str,
    report: RepairReport,
) -> None:
    """Move ``rel`` under the quarantine prefix; never delete without a move."""
    key = rel.replace("\\", "/").lstrip("/")
    dest = f"{quarantine_prefix}/{key}"
    try:
        await storage.move(key, dest)
    except Exception as exc:
        logger.warning("memory repair: quarantine move failed %s → %s: %r", key, dest, exc)
        return
    report.quarantined.append(key)
    logger.warning("memory repair: quarantined unreadable file %s → %s", key, dest)


async def _try_read(storage: WorkspaceStorage, path: str) -> str | None:
    """Return text, or ``None`` if missing / unreadable (encoding or I/O)."""
    try:
        if not await storage.exists(path):
            return None
        return await storage.read_text(path)
    except (UnicodeDecodeError, UnicodeError, OSError, ValueError) as exc:
        logger.warning("memory repair: unreadable %s: %r", path, exc)
        return None
    except Exception as exc:
        # Broad catch: remote backends may raise SDK-specific errors.
        logger.warning("memory repair: read failed %s: %r", path, exc)
        return None


async def _list_typed_notes(storage: WorkspaceStorage) -> list[str]:
    notes: list[str] = []
    for folder in TYPED_FOLDERS:
        try:
            paths = await storage.list_files(f"{folder}/")
        except Exception as exc:
            logger.warning("memory repair: list %s/ failed: %r", folder, exc)
            continue
        for rel in paths:
            rel_posix = rel.replace("\\", "/").lstrip("/")
            if not rel_posix.startswith(f"{folder}/") or not rel_posix.endswith(".md"):
                continue
            if rel_posix.count("/") != 1:
                continue
            notes.append(rel_posix)
    return notes


async def _scan_typed_notes(
    storage: WorkspaceStorage,
    quarantine_prefix: str,
    report: RepairReport,
) -> dict[str, str]:
    """Read typed notes; quarantine unreadable files. Return path → text."""
    readable: dict[str, str] = {}
    for path in await _list_typed_notes(storage):
        text = await _try_read(storage, path)
        if text is None:
            if await storage.exists(path):
                await _quarantine(storage, path, quarantine_prefix, report)
            continue
        readable[path] = text
    return readable


async def _rebuild_index_from_notes(
    storage: WorkspaceStorage,
    readable: dict[str, str],
) -> list[str]:
    """Build INDEX entry lines from readable active notes (path-sorted)."""
    entries: list[str] = []
    for path in sorted(readable):
        text = readable[path]
        meta, body = parse_memory_note(text)
        if meta is not None and meta.status in ("forgotten", "superseded"):
            continue
        folder, _, filename = path.partition("/")
        if not folder or not filename:
            continue
        entries.append(format_index_entry_line(folder, filename, _summary_from_body(body)))
    await storage.write_text(
        INDEX_FILENAME,
        format_index_document(DEFAULT_INDEX_HEADER, entries),
    )
    return entries


async def _prune_index(
    storage: WorkspaceStorage,
    *,
    drop: set[str],
    existing_raw: str,
) -> tuple[list[str], list[str]]:
    """Drop INDEX rows whose targets are in ``drop`` or missing on disk."""
    header, entries = split_index_document(existing_raw)
    kept: list[str] = []
    pruned: list[str] = []
    for line in entries:
        target = wiki_target_from_line(line)
        if not is_index_entry_line(line):
            if target and target in drop:
                pruned.append(target)
                continue
            kept.append(line)
            continue
        if target is None:
            kept.append(line)
            continue
        if target in drop:
            pruned.append(target)
            continue
        if not await storage.exists(target):
            pruned.append(target)
            continue
        kept.append(line)
    await storage.write_text(INDEX_FILENAME, format_index_document(header, kept))
    return kept, pruned


async def repair_memory_tree(
    storage: WorkspaceStorage,
    *,
    full_scan: bool = False,
) -> RepairReport:
    """Quarantine unreadable memory files and restore a usable ``INDEX.md``.

    Fast path (``full_scan=False``, default for turn load):
      - Readable INDEX → no-op (does not open typed notes).
      - Unreadable / missing INDEX → scan notes, quarantine, rebuild.

    Full scan (CLI / organizer INDEX-read failure):
      - Always scan typed notes; quarantine bad files; prune or rebuild INDEX.

    Never deletes without moving into ``.quarantine/<utc_ts>/``.
    """
    report = RepairReport()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_prefix = f"{_QUARANTINE_ROOT}/{ts}"

    index_exists = await storage.exists(INDEX_FILENAME)
    index_raw: str | None = None
    index_unreadable = False
    if index_exists:
        index_raw = await _try_read(storage, INDEX_FILENAME)
        if index_raw is None:
            index_unreadable = True
            await _quarantine(storage, INDEX_FILENAME, quarantine_prefix, report)

    # Healthy INDEX on the hot path: skip note I/O entirely.
    if index_raw is not None and not full_scan:
        return report

    readable = await _scan_typed_notes(storage, quarantine_prefix, report)
    notes_quarantined = {q for q in report.quarantined if q.startswith(_TYPED_PREFIXES)}

    if index_unreadable or not await storage.exists(INDEX_FILENAME):
        entries = await _rebuild_index_from_notes(storage, readable)
        report.index_rebuilt = True
        report.entries_written = len(entries)
        return report

    if notes_quarantined and index_raw is not None:
        kept, pruned = await _prune_index(
            storage, drop=notes_quarantined, existing_raw=index_raw
        )
        report.index_pruned = pruned
        report.entries_written = len(kept)

    return report


__all__ = ["RepairReport", "repair_memory_tree"]
