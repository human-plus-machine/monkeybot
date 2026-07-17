"""Local SQLite vector store for knowledge ANN (brute-force cosine).

Single-writer assumption: one gateway process owns the DB. Fine for experiment
scale (~thousands of chunks); loads all vectors into memory per query.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from monkeybot.core.lockfile_names import LOCKFILE_NAMES

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    chunk_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'workspace_file',
    start_line INTEGER,
    end_line INTEGER,
    model_id TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_path ON vectors(path);
"""

_BUSY_TIMEOUT_MS = 5000
_BRUTE_FORCE_WARN_ROWS = 5000


@dataclass(frozen=True)
class VectorHit:
    """One ANN similarity hit (chunk-keyed)."""

    chunk_id: str
    path: str
    score: float
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class VectorChunkRecord:
    """One chunk vector to upsert."""

    chunk_id: str
    path: str
    vector: list[float]
    model_id: str
    dim: int
    start_line: int | None = None
    end_line: int | None = None
    source_type: str = "workspace_file"
    text: str | None = None


class SQLiteVectorStore:
    """Brute-force cosine similarity over float32 vectors in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info("knowledge vector store open path=%s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("knowledge vector store closed path=%s", self._path)

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteVectorStore is not open")
        return self._conn

    async def upsert(self, chunks: list[VectorChunkRecord]) -> None:
        if not chunks:
            return
        # Never persist zero / empty vectors (poison ANN scores).
        valid = [c for c in chunks if c.vector and any(abs(x) > 0.0 for x in c.vector)]
        skipped = len(chunks) - len(valid)
        if skipped:
            logger.warning(
                "knowledge vector upsert skipped %d empty/zero vectors path_sample=%s",
                skipped,
                chunks[0].path if chunks else "",
            )
        if not valid:
            return
        conn = self._require()
        rows = [
            (
                c.chunk_id,
                c.path,
                c.source_type,
                c.start_line,
                c.end_line,
                c.model_id,
                c.dim,
                _pack_f32(c.vector),
            )
            for c in valid
        ]
        await conn.executemany(
            """
            INSERT INTO vectors (
                chunk_id, path, source_type, start_line, end_line,
                model_id, dim, vector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                path=excluded.path,
                source_type=excluded.source_type,
                start_line=excluded.start_line,
                end_line=excluded.end_line,
                model_id=excluded.model_id,
                dim=excluded.dim,
                vector=excluded.vector
            """,
            rows,
        )
        await conn.commit()
        logger.debug("knowledge vector upsert n=%d", len(valid))

    async def delete_by_path(self, path: str) -> None:
        conn = self._require()
        await conn.execute("DELETE FROM vectors WHERE path = ?", (path,))
        await conn.commit()

    async def delete_missing(self, alive_paths: set[str]) -> int:
        conn = self._require()
        cur = await conn.execute("SELECT DISTINCT path FROM vectors")
        rows = await cur.fetchall()
        stale = [str(r["path"]) for r in rows if str(r["path"]) not in alive_paths]
        if not stale:
            return 0
        await conn.executemany(
            "DELETE FROM vectors WHERE path = ?",
            [(p,) for p in stale],
        )
        await conn.commit()
        return len(stale)

    async def has_path(self, path: str) -> bool:
        conn = self._require()
        cur = await conn.execute(
            "SELECT 1 FROM vectors WHERE path = ? LIMIT 1", (path,)
        )
        row = await cur.fetchone()
        return row is not None

    async def query(
        self,
        vector: list[float],
        *,
        limit: int = 20,
        path_prefix: str | None = None,
        dimensions: int | None = None,
    ) -> list[VectorHit]:
        conn = self._require()
        if path_prefix:
            cur = await conn.execute(
                "SELECT chunk_id, path, start_line, end_line, dim, vector "
                "FROM vectors WHERE path LIKE ?",
                (f"{path_prefix}%",),
            )
        else:
            cur = await conn.execute(
                "SELECT chunk_id, path, start_line, end_line, dim, vector FROM vectors"
            )
        rows = await cur.fetchall()
        row_list = list(rows)
        if not row_list:
            logger.debug("knowledge vector query empty table")
            return []
        if len(row_list) >= _BRUTE_FORCE_WARN_ROWS:
            logger.warning(
                "knowledge vector brute-force scan rows=%d (consider a real ANN index)",
                len(row_list),
            )

        packed: list[tuple[str, str, int | None, int | None, int, bytes]] = [
            (
                str(row["chunk_id"]),
                str(row["path"]),
                int(row["start_line"]) if row["start_line"] is not None else None,
                int(row["end_line"]) if row["end_line"] is not None else None,
                int(row["dim"]),
                bytes(row["vector"]),
            )
            for row in row_list
        ]
        use_dim = dimensions if dimensions and dimensions > 0 else None
        hits = await asyncio.to_thread(
            _score_rows,
            packed,
            list(vector),
            max(1, limit),
            use_dim,
        )
        logger.debug(
            "knowledge vector query rows=%d hits=%d limit=%d",
            len(row_list),
            len(hits),
            limit,
        )
        return hits


def _score_rows(
    rows: list[tuple[str, str, int | None, int | None, int, bytes]],
    vector: list[float],
    limit: int,
    use_dim: int | None,
) -> list[VectorHit]:
    if use_dim is not None:
        q = _l2_normalize(vector[:use_dim])
    else:
        q = _l2_normalize(vector)

    best_by_path: dict[str, VectorHit] = {}
    for chunk_id, path, start_line, end_line, dim, blob in rows:
        base = path.rsplit("/", 1)[-1].lower()
        if base in LOCKFILE_NAMES or base.endswith(".lock"):
            continue
        stored = _unpack_f32(blob, dim)
        if use_dim is not None:
            stored = _l2_normalize(stored[:use_dim])
        elif abs(sum(x * x for x in stored) - 1.0) > 0.05:
            stored = _l2_normalize(stored)
        score = _dot(q, stored)
        hit = VectorHit(
            chunk_id=chunk_id,
            path=path,
            score=score,
            start_line=start_line,
            end_line=end_line,
        )
        prev = best_by_path.get(path)
        if prev is None or hit.score > prev.score:
            best_by_path[path] = hit

    scored = sorted(best_by_path.values(), key=lambda h: (-h.score, h.path))
    return scored[:limit]


def _pack_f32(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def _unpack_f32(blob: bytes, dim: int) -> list[float]:
    n = len(blob) // 4
    vals = list(struct.unpack(f"{n}f", blob))
    if n == dim:
        return vals
    if n > dim:
        return vals[:dim]
    return vals + [0.0] * (dim - n)


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return list(vec)
    return [x / norm for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


__all__ = ["SQLiteVectorStore", "VectorChunkRecord", "VectorHit"]
