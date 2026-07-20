"""Memory-local SQLite graph sidecar (nodes + wiki / supersedes edges)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    link_type TEXT NOT NULL,
    PRIMARY KEY (source_path, target_path, link_type)
);
CREATE INDEX IF NOT EXISTS idx_mem_links_source ON links(source_path);
CREATE INDEX IF NOT EXISTS idx_mem_links_target ON links(target_path);
CREATE INDEX IF NOT EXISTS idx_mem_notes_status ON notes(status);
"""


class MemoryGraph:
    """Process-local graph for durable memory notes (not the knowledge DB)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("memory graph open path=%s", self._db_path)

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("MemoryGraph is not open")
        return self._conn

    async def upsert_note(
        self,
        path: str,
        *,
        note_type: str,
        status: str,
        updated_at: float,
        links: list[tuple[str, str]] | None = None,
    ) -> None:
        path = path.replace("\\", "/")
        await self._db.execute(
            """
            INSERT INTO notes(path, type, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                type=excluded.type,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (path, note_type, status, updated_at),
        )
        await self._db.execute("DELETE FROM links WHERE source_path = ?", (path,))
        if links:
            await self._db.executemany(
                "INSERT OR IGNORE INTO links(source_path, target_path, link_type) VALUES (?, ?, ?)",
                [(path, tgt, lt) for tgt, lt in links],
            )
        await self._db.commit()
        logger.debug(
            "memory graph upsert path=%s type=%s status=%s links=%d",
            path,
            note_type,
            status,
            len(links or []),
        )

    async def set_status(self, path: str, status: str, *, updated_at: float) -> None:
        path = path.replace("\\", "/")
        await self._db.execute(
            "UPDATE notes SET status = ?, updated_at = ? WHERE path = ?",
            (status, updated_at, path),
        )
        await self._db.commit()

    async def delete_note(self, path: str) -> None:
        path = path.replace("\\", "/")
        await self._db.execute(
            "DELETE FROM links WHERE source_path = ? OR target_path = ?", (path, path)
        )
        await self._db.execute("DELETE FROM notes WHERE path = ?", (path,))
        await self._db.commit()
        logger.info("memory graph delete path=%s", path)

    async def get_updated_at(self, path: str) -> float | None:
        path = path.replace("\\", "/")
        cur = await self._db.execute(
            "SELECT updated_at FROM notes WHERE path = ?", (path,)
        )
        row = await cur.fetchone()
        return float(row[0]) if row else None

    async def get_status(self, path: str) -> str | None:
        path = path.replace("\\", "/")
        cur = await self._db.execute("SELECT status FROM notes WHERE path = ?", (path,))
        row = await cur.fetchone()
        return str(row[0]) if row else None

    async def neighbors(self, path: str) -> list[str]:
        path = path.replace("\\", "/")
        cur = await self._db.execute(
            """
            SELECT target_path FROM links WHERE source_path = ?
            UNION
            SELECT source_path FROM links WHERE target_path = ?
            """,
            (path, path),
        )
        rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    async def list_paths(self, *, status: str | None = "active") -> list[str]:
        if status is None:
            cur = await self._db.execute("SELECT path FROM notes ORDER BY path")
        else:
            cur = await self._db.execute(
                "SELECT path FROM notes WHERE status = ? ORDER BY path", (status,)
            )
        rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    async def export_graph(self) -> dict[str, Any]:
        cur = await self._db.execute("SELECT path, type, status FROM notes ORDER BY path")
        nodes = [
            {"id": r[0], "path": r[0], "type": r[1], "status": r[2]}
            for r in await cur.fetchall()
        ]
        cur = await self._db.execute(
            "SELECT source_path, target_path, link_type FROM links ORDER BY source_path"
        )
        edges = [
            {"source": r[0], "target": r[1], "link_type": r[2]}
            for r in await cur.fetchall()
        ]
        return {"nodes": nodes, "edges": edges}


__all__ = ["MemoryGraph"]
