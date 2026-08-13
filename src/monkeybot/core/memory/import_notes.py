"""Idempotent importer for legacy INDEX.md / typed markdown notes."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from monkeybot.core.memory.ids import utc_now_iso
from monkeybot.core.memory.palace import CONVERSATION_ROOM, PalacePort

logger = logging.getLogger(__name__)

_TYPED_FOLDERS = ("episodic", "semantic", "procedural", "working")
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<meta>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)
_IMPORT_MARKER = ".notes_imported"


def _note_drawer_id(rel_posix: str) -> str:
    payload = rel_posix.encode()
    return "note_" + hashlib.sha256(payload).hexdigest()


def _legacy_note_drawer_id(agent_id: str, rel_posix: str) -> str:
    payload = f"{agent_id}\0{rel_posix}".encode()
    return "note_" + hashlib.sha256(payload).hexdigest()


def _already_imported(palace: PalacePort, *, agent_id: str, rel_posix: str) -> bool:
    return (
        palace.get_drawer(_note_drawer_id(rel_posix)) is not None
        or palace.get_drawer(_legacy_note_drawer_id(agent_id, rel_posix)) is not None
    )


def _parse_note(text: str) -> tuple[str, str, str]:
    """Return (note_type, status, body)."""
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return "episodic", "active", text or ""
    fields: dict[str, str] = {}
    for line in match.group("meta").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip().lower()] = val.strip()
    note_type = fields.get("type", "").strip().lower() or "episodic"
    status = fields.get("status", "active").strip().lower() or "active"
    return note_type, status, match.group("body")


def _legacy_notes_root(palace: PalacePort) -> Path | None:
    palace_path = Path(palace.palace_path)
    parent = palace_path.parent
    if parent.name == "memory":
        return parent
    candidate = parent / "memory"
    if candidate.is_dir():
        return candidate
    return None


def import_legacy_notes(palace: PalacePort, *, agent_id: str) -> int:
    """Copy leftover markdown notes into the palace. Idempotent; never deletes sources."""
    marker = Path(palace.palace_path) / _IMPORT_MARKER
    if marker.is_file():
        return 0
    root = _legacy_notes_root(palace)
    if root is None or not root.is_dir():
        return 0
    imported = 0
    complete = True
    for folder in _TYPED_FOLDERS:
        folder_path = root / folder
        if not folder_path.is_dir():
            continue
        for path in sorted(folder_path.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("legacy note read failed %s: %r", path, exc)
                complete = False
                continue
            note_type, status, body = _parse_note(text)
            if status == "forgotten":
                continue
            body = body.strip()
            if not body:
                continue
            drawer_id = _note_drawer_id(rel)
            if _already_imported(palace, agent_id=agent_id, rel_posix=rel):
                continue
            filed = utc_now_iso()
            try:
                mtime = path.stat().st_mtime

                filed = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")
            except OSError:
                pass
            try:
                with palace.acquire_write_lock():
                    palace.upsert_drawer(
                        drawer_id,
                        body,
                        {
                            "wing": "main",
                            "room": CONVERSATION_ROOM,
                            "thread_id": "legacy-notes",
                            "role": "system",
                            "source": "legacy_note",
                            "legacy_path": rel,
                            "note_type": note_type,
                            "status": status,
                            "agent_id": agent_id,
                            "filed_at": filed,
                            "source_timestamp": filed,
                            "added_by": "monkeybot-import",
                        },
                    )
            except Exception as exc:
                logger.warning("legacy note upsert failed %s: %r", rel, exc)
                complete = False
                continue
            imported += 1
    index_path = root / "INDEX.md"
    if index_path.is_file():
        try:
            index_text = index_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("legacy INDEX read failed: %r", exc)
            complete = False
            index_text = ""
        if index_text:
            drawer_id = _note_drawer_id("INDEX.md")
            if not _already_imported(palace, agent_id=agent_id, rel_posix="INDEX.md"):
                try:
                    with palace.acquire_write_lock():
                        palace.upsert_drawer(
                            drawer_id,
                            index_text,
                            {
                                "wing": "main",
                                "room": CONVERSATION_ROOM,
                                "thread_id": "legacy-notes",
                                "role": "system",
                                "source": "legacy_index",
                                "legacy_path": "INDEX.md",
                                "agent_id": agent_id,
                                "filed_at": utc_now_iso(),
                                "source_timestamp": utc_now_iso(),
                                "added_by": "monkeybot-import",
                            },
                        )
                    imported += 1
                except Exception as exc:
                    logger.warning("legacy INDEX import failed: %r", exc)
                    complete = False
    if complete:
        try:
            marker.write_text(f"imported={imported}\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("could not write import marker: %r", exc)
    return imported


def migrate_memory_uri_in_yaml(yaml_path: Path) -> bool:
    """Rewrite legacy ``local://./memory`` URIs to the MemPalace root. Keeps a .bak."""
    if not yaml_path.is_file():
        return False
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return False
    replacements = (
        ("local://./memory\n", "local://./memory/mempalace\n"),
        ("local://./memory ", "local://./memory/mempalace "),
        ("local://memory\n", "local://./memory/mempalace\n"),
        ("local://./data/memory\n", "local://./memory/mempalace\n"),
    )
    new = text
    for old, repl in replacements:
        # Only replace exact memory root, not already-migrated mempalace URIs.
        if "memory/mempalace" in old:
            continue
        new = new.replace(old, repl)
    # Avoid turning mempalace into mempalace/mempalace
    new = new.replace("local://./memory/mempalace/mempalace", "local://./memory/mempalace")
    if new == text:
        return False
    bak = yaml_path.with_suffix(yaml_path.suffix + ".bak-pre-mempalace")
    try:
        if not bak.exists():
            bak.write_text(text, encoding="utf-8")
        yaml_path.write_text(new, encoding="utf-8")
    except OSError as exc:
        logger.warning("memory URI migrate failed for %s: %r", yaml_path, exc)
        return False
    return True
