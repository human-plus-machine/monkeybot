"""Memory mutation helpers: edit / update (supersede) / forget."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from monkeybot.core.memory.graph import MemoryGraph
from monkeybot.core.memory.index_format import (
    INDEX_FILENAME,
    append_index_entries,
    format_index_document,
    is_index_entry_line,
    split_index_document,
)
from monkeybot.core.memory.note_format import (
    TYPED_FOLDERS,
    extract_memory_wiki_links,
    folder_from_rel_path,
    format_memory_note,
    parse_memory_note,
)
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)


def _index_line(folder: str, filename: str, summary: str) -> str:
    one = " ".join(summary.strip().split())
    if len(one) > 160:
        one = one[:157] + "..."
    return f"- [[{folder}/{filename}]] | tags: | summary: {one}"


async def drop_index_paths(storage: WorkspaceStorage, drop: set[str]) -> None:
    """Remove INDEX.md rows whose wiki targets are in ``drop``."""
    if not drop or not await storage.exists(INDEX_FILENAME):
        return
    raw = await storage.read_text(INDEX_FILENAME)
    header, entries = split_index_document(raw)
    kept: list[str] = []
    for line in entries:
        if not is_index_entry_line(line):
            kept.append(line)
            continue
        start = line.find("[[")
        end = line.find("]]", start)
        if start < 0 or end < 0:
            kept.append(line)
            continue
        target = line[start + 2 : end].strip()
        if target in drop:
            continue
        kept.append(line)
    await storage.write_text(INDEX_FILENAME, format_index_document(header, kept))


async def _upsert_graph_from_text(
    graph: MemoryGraph | None,
    path: str,
    text: str,
) -> None:
    if graph is None:
        return
    meta, _body = parse_memory_note(text)
    if meta is None:
        return
    links = [(t, "related") for t in extract_memory_wiki_links(text)]
    if meta.supersedes:
        links.append((meta.supersedes, "supersedes"))
    await graph.upsert_note(
        path,
        note_type=meta.type,
        status=meta.status,
        updated_at=time.time(),
        links=links,
    )


async def edit_memory_note(
    storage: WorkspaceStorage,
    graph: MemoryGraph | None,
    *,
    path: str,
    content: str,
) -> dict[str, Any]:
    path = path.replace("\\", "/").lstrip("./")
    folder = folder_from_rel_path(path)
    if folder is None:
        return {"ok": False, "error": "path must be under episodic|semantic|procedural|working"}
    if not await storage.exists(path):
        return {"ok": False, "error": f"note not found: {path}"}
    existing = await storage.read_text(path)
    old_meta, _ = parse_memory_note(existing)
    note_type = old_meta.type if old_meta else folder
    text = format_memory_note(note_type=note_type, status="active", body=content)
    await storage.write_text(path, text)
    await _upsert_graph_from_text(graph, path, text)
    logger.info("memory edit path=%s", path)
    return {"ok": True, "path": path, "action": "edit"}


async def update_memory_note(
    storage: WorkspaceStorage,
    graph: MemoryGraph | None,
    *,
    path: str,
    content: str,
) -> dict[str, Any]:
    path = path.replace("\\", "/").lstrip("./")
    folder = folder_from_rel_path(path) or "semantic"
    if folder not in TYPED_FOLDERS:
        return {"ok": False, "error": "path must be under episodic|semantic|procedural|working"}
    if not await storage.exists(path):
        return {"ok": False, "error": f"note not found: {path}"}

    old_text = await storage.read_text(path)
    old_meta, _ = parse_memory_note(old_text)
    note_type = old_meta.type if old_meta else folder

    superseded = format_memory_note(
        note_type=note_type,
        status="superseded",
        body=parse_memory_note(old_text)[1] if old_meta else old_text,
    )
    await storage.write_text(path, superseded)
    await _upsert_graph_from_text(graph, path, superseded)

    filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.md"
    new_path = f"{folder}/{filename}"
    new_text = format_memory_note(
        note_type=note_type,
        status="active",
        body=content,
        supersedes=path,
    )
    await storage.write_text(new_path, new_text)
    await _upsert_graph_from_text(graph, new_path, new_text)

    await drop_index_paths(storage, {path})
    summary = " ".join(content.strip().split())[:160]
    if await storage.exists(INDEX_FILENAME):
        existing_idx = await storage.read_text(INDEX_FILENAME)
    else:
        existing_idx = "# Memory Index\n"
    merged = append_index_entries(existing_idx, [_index_line(folder, filename, summary)])
    await storage.write_text(INDEX_FILENAME, merged)

    logger.info("memory update superseded=%s path=%s", path, new_path)
    return {
        "ok": True,
        "path": new_path,
        "superseded": path,
        "action": "update",
    }


async def forget_memory_note(
    storage: WorkspaceStorage,
    graph: MemoryGraph | None,
    *,
    path: str,
) -> dict[str, Any]:
    path = path.replace("\\", "/").lstrip("./")
    if folder_from_rel_path(path) is None:
        return {"ok": False, "error": "path must be under episodic|semantic|procedural|working"}
    if not await storage.exists(path):
        return {"ok": False, "error": f"note not found: {path}"}
    old = await storage.read_text(path)
    meta, body = parse_memory_note(old)
    note_type = meta.type if meta else (folder_from_rel_path(path) or "episodic")
    forgotten = format_memory_note(note_type=note_type, status="forgotten", body=body)
    await storage.write_text(path, forgotten)
    await _upsert_graph_from_text(graph, path, forgotten)
    await drop_index_paths(storage, {path})
    logger.info("memory forget path=%s", path)
    return {"ok": True, "path": path, "action": "forget"}


__all__ = [
    "drop_index_paths",
    "edit_memory_note",
    "forget_memory_note",
    "update_memory_note",
]
