"""Local SQLite vector store for knowledge ANN (brute-force cosine).

Single-writer assumption: one gateway process owns the DB. Vectors are
L2-normalized once at write time (``upsert``); an in-memory numpy matrix of
all rows is cached and reused across queries, invalidated on any write
(``upsert`` / ``delete_by_path`` / ``delete_missing``). This keeps repeat
queries to a single ``matrix @ query`` instead of re-unpacking and
re-normalizing every row from SQLite each time.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import numpy as np

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
_BRUTE_FORCE_WARN_ROWS = 20_000


def _sqlite_uri(path: Path, *, read_only: bool) -> str:
    resolved = path.resolve().as_posix()
    if not resolved.startswith("/"):
        resolved = "/" + resolved
    mode = "ro" if read_only else "rwc"
    return f"file:{resolved}?mode={mode}"


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


@dataclass(frozen=True)
class _MatrixCache:
    """All rows unpacked once into a dense, row-normalized numpy matrix."""

    chunk_ids: list[str]
    paths: list[str]
    start_lines: list[int | None]
    end_lines: list[int | None]
    matrix: np.ndarray  # shape (n, dim), float32, each row unit-normalized


class SQLiteVectorStore:
    """Brute-force cosine similarity over float32 vectors in SQLite.

    One gateway writer owns mutations. Subagents / readers open with
    ``read_only=True`` (SQLite ``mode=ro``) and may only ``query``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._cache: _MatrixCache | None = None
        self._cache_lock = asyncio.Lock()
        self._read_only = False
        # Bumped on every write; lets a concurrent cache rebuild detect that
        # the rows it read are already stale and must not be cached (avoids
        # losing an invalidation to an in-flight build — see _get_cache).
        self._write_version = 0

    async def open(self, *, read_only: bool = False) -> None:
        if self._conn is not None:
            return
        self._read_only = read_only
        if read_only:
            if not self._path.is_file():
                raise FileNotFoundError(
                    f"knowledge vector store not found for read-only open: {self._path}"
                )
            uri = _sqlite_uri(self._path, read_only=True)
            self._conn = await aiosqlite.connect(uri, uri=True)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            logger.info("knowledge vector store open (read-only) path=%s", self._path)
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
            self._invalidate_cache()
            logger.info("knowledge vector store closed path=%s", self._path)

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteVectorStore is not open")
        return self._conn

    def _require_writable(self) -> aiosqlite.Connection:
        if self._read_only:
            raise RuntimeError(
                "SQLiteVectorStore is open read-only; writes require the gateway writer"
            )
        return self._require()

    def _invalidate_cache(self) -> None:
        self._cache = None
        self._write_version += 1

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
        conn = self._require_writable()
        rows = [
            (
                c.chunk_id,
                c.path,
                c.source_type,
                c.start_line,
                c.end_line,
                c.model_id,
                c.dim,
                # Normalize once here so every downstream read (cache build,
                # brute-force query) can trust vectors are already unit-length.
                _pack_f32(_l2_normalize(c.vector)),
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
        self._invalidate_cache()
        logger.debug("knowledge vector upsert n=%d", len(valid))

    async def delete_by_path(self, path: str) -> None:
        conn = self._require_writable()
        await conn.execute("DELETE FROM vectors WHERE path = ?", (path,))
        await conn.commit()
        self._invalidate_cache()

    async def delete_missing(self, alive_paths: set[str]) -> int:
        conn = self._require_writable()
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
        self._invalidate_cache()
        return len(stale)

    async def has_path(self, path: str) -> bool:
        conn = self._require()
        cur = await conn.execute("SELECT 1 FROM vectors WHERE path = ? LIMIT 1", (path,))
        row = await cur.fetchone()
        return row is not None

    async def _get_cache(self) -> _MatrixCache:
        """Return the cached row matrix, rebuilding it if invalidated.

        Rebuild cost (unpack + normalize every row) is paid once per write
        burst rather than once per query.
        """
        async with self._cache_lock:
            if self._cache is not None:
                return self._cache
            version = self._write_version
            conn = self._require()
            cur = await conn.execute(
                "SELECT chunk_id, path, start_line, end_line, dim, vector FROM vectors"
            )
            rows = await cur.fetchall()
            row_list = list(rows)
            if len(row_list) >= _BRUTE_FORCE_WARN_ROWS:
                logger.warning(
                    "knowledge vector cache rebuild rows=%d (consider a real ANN index)",
                    len(row_list),
                )
            cache = await asyncio.to_thread(_build_cache, row_list)
            # A write may have committed while we were reading/building above;
            # its invalidation already fired before we re-check, so only
            # install the cache if no write raced us, otherwise leave it
            # unset so the next query rebuilds from fresh rows.
            if self._write_version == version:
                self._cache = cache
            return cache

    async def query(
        self,
        vector: list[float],
        *,
        limit: int = 20,
        path_prefix: str | None = None,
        dimensions: int | None = None,
    ) -> list[VectorHit]:
        cache = await self._get_cache()
        n = len(cache.chunk_ids)
        if n == 0:
            logger.debug("knowledge vector query empty table")
            return []

        use_dim = dimensions if dimensions and dimensions > 0 else None
        hits = await asyncio.to_thread(
            _score_cache,
            cache,
            list(vector),
            max(1, limit),
            use_dim,
            path_prefix,
        )
        logger.debug(
            "knowledge vector query rows=%d hits=%d limit=%d",
            n,
            len(hits),
            limit,
        )
        return hits


def _build_cache(
    rows: list[aiosqlite.Row],
) -> _MatrixCache:
    chunk_ids: list[str] = []
    paths: list[str] = []
    start_lines: list[int | None] = []
    end_lines: list[int | None] = []
    vecs: list[list[float]] = []
    max_dim = 0
    for row in rows:
        base = str(row["path"]).rsplit("/", 1)[-1].lower()
        if base in LOCKFILE_NAMES or base.endswith(".lock"):
            continue
        dim = int(row["dim"])
        vec = _unpack_f32(bytes(row["vector"]), dim)
        max_dim = max(max_dim, len(vec))
        chunk_ids.append(str(row["chunk_id"]))
        paths.append(str(row["path"]))
        start_lines.append(int(row["start_line"]) if row["start_line"] is not None else None)
        end_lines.append(int(row["end_line"]) if row["end_line"] is not None else None)
        vecs.append(vec)

    if not vecs:
        return _MatrixCache(
            chunk_ids=[],
            paths=[],
            start_lines=[],
            end_lines=[],
            matrix=np.zeros((0, 0), dtype=np.float32),
        )

    matrix = np.zeros((len(vecs), max_dim), dtype=np.float32)
    for i, vec in enumerate(vecs):
        matrix[i, : len(vec)] = vec
    _normalize_rows_inplace(matrix)

    return _MatrixCache(
        chunk_ids=chunk_ids,
        paths=paths,
        start_lines=start_lines,
        end_lines=end_lines,
        matrix=matrix,
    )


def _normalize_rows_inplace(matrix: np.ndarray) -> None:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    np.divide(matrix, norms, out=matrix, where=norms > 0.0)


def _score_cache(
    cache: _MatrixCache,
    vector: list[float],
    limit: int,
    use_dim: int | None,
    path_prefix: str | None,
) -> list[VectorHit]:
    if cache.matrix.size == 0:
        return []

    if use_dim is not None and use_dim < cache.matrix.shape[1]:
        # One configured Matryoshka width per store — slice+renorm per query
        # (cheap vs matmul); no multi-width cache.
        width = min(use_dim, cache.matrix.shape[1])
        matrix = np.array(cache.matrix[:, :width], dtype=np.float32, copy=True)
        _normalize_rows_inplace(matrix)
        q = _l2_normalize(vector[:use_dim])
    else:
        matrix = cache.matrix
        q = _l2_normalize(vector)

    q_arr = np.zeros(matrix.shape[1], dtype=np.float32)
    q_arr[: min(len(q), matrix.shape[1])] = q[: matrix.shape[1]]
    scores = matrix @ q_arr

    best_by_path: dict[str, VectorHit] = {}
    for i in np.argsort(-scores):
        path = cache.paths[i]
        if path_prefix and not path.startswith(path_prefix):
            continue
        hit = VectorHit(
            chunk_id=cache.chunk_ids[i],
            path=path,
            score=float(scores[i]),
            start_line=cache.start_lines[i],
            end_line=cache.end_lines[i],
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


__all__ = ["SQLiteVectorStore", "VectorChunkRecord", "VectorHit"]
