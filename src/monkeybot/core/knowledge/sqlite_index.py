"""Local SQLite sidecar: files, chunks, FTS5, and link graph."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, cast

import aiosqlite

from monkeybot.core.knowledge.links import ParsedLink, format_link_ref
from monkeybot.core.knowledge.types import LinkType, SourceType, TextChunk

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    mtime REAL,
    content_hash TEXT NOT NULL,
    modality TEXT NOT NULL DEFAULT 'text'
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    source_type TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    text TEXT NOT NULL,
    FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    path UNINDEXED,
    source_type UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS links (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    link_type TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    PRIMARY KEY (source_path, target_path, link_type, start_line, end_line)
);

CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_path);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_path);
"""


class KnowledgeIndex:
    """Async SQLite FTS + links store for the knowledge layer."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._db_path

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("KnowledgeIndex.open() has not been called")
        return self._conn

    async def get_file_hash(self, path: str) -> str | None:
        conn = self._require()
        cur = await conn.execute("SELECT content_hash FROM files WHERE path = ?", (path,))
        row = await cur.fetchone()
        return str(row["content_hash"]) if row else None

    async def get_file_mtime(self, path: str) -> float | None:
        conn = self._require()
        cur = await conn.execute("SELECT mtime FROM files WHERE path = ?", (path,))
        row = await cur.fetchone()
        if row is None or row["mtime"] is None:
            return None
        return float(row["mtime"])

    async def counts(self) -> tuple[int, int]:
        """Return ``(indexed_files, indexed_chunks)`` for salience reporting."""
        conn = self._require()
        cur = await conn.execute("SELECT COUNT(*) AS n FROM files")
        row = await cur.fetchone()
        files = int(row["n"]) if row else 0
        cur = await conn.execute("SELECT COUNT(*) AS n FROM chunks")
        row = await cur.fetchone()
        chunks = int(row["n"]) if row else 0
        return files, chunks

    async def list_paths(self, *, source_type: SourceType | None = None) -> set[str]:
        conn = self._require()
        if source_type is None:
            cur = await conn.execute("SELECT path FROM files")
        else:
            cur = await conn.execute(
                "SELECT path FROM files WHERE source_type = ?", (source_type,)
            )
        rows = await cur.fetchall()
        return {str(r["path"]) for r in rows}

    async def upsert_file(
        self,
        *,
        path: str,
        source_type: SourceType,
        content_hash: str,
        mtime: float | None,
        chunks: list[TextChunk],
        links: list[ParsedLink] | None = None,
    ) -> None:
        conn = self._require()
        await conn.execute("BEGIN")
        try:
            await conn.execute(
                """
                INSERT INTO files (path, source_type, mtime, content_hash, modality)
                VALUES (?, ?, ?, ?, 'text')
                ON CONFLICT(path) DO UPDATE SET
                    source_type = excluded.source_type,
                    mtime = excluded.mtime,
                    content_hash = excluded.content_hash
                """,
                (path, source_type, mtime, content_hash),
            )
            await self._delete_chunks_for_path(path)
            await conn.execute("DELETE FROM links WHERE source_path = ?", (path,))

            if chunks:
                chunk_rows = [
                    (
                        chunk.chunk_id,
                        chunk.path,
                        chunk.source_type,
                        chunk.start_line,
                        chunk.end_line,
                        chunk.text,
                    )
                    for chunk in chunks
                ]
                fts_rows = [
                    (chunk.chunk_id, chunk.path, chunk.source_type, chunk.text)
                    for chunk in chunks
                ]
                await conn.executemany(
                    """
                    INSERT INTO chunks (id, path, source_type, start_line, end_line, text)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    chunk_rows,
                )
                await conn.executemany(
                    """
                    INSERT INTO chunks_fts (chunk_id, path, source_type, text)
                    VALUES (?, ?, ?, ?)
                    """,
                    fts_rows,
                )

            if links:
                link_rows = [
                    (
                        path,
                        link.target_path,
                        link.link_type,
                        link.start_line,
                        link.end_line,
                    )
                    for link in links
                ]
                await conn.executemany(
                    """
                    INSERT OR IGNORE INTO links
                        (source_path, target_path, link_type, start_line, end_line)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    link_rows,
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def delete_path(self, path: str) -> None:
        conn = self._require()
        await conn.execute("BEGIN")
        try:
            await self._delete_chunks_for_path(path)
            await conn.execute("DELETE FROM links WHERE source_path = ?", (path,))
            await conn.execute("DELETE FROM files WHERE path = ?", (path,))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def delete_missing(self, alive_paths: set[str]) -> int:
        """Remove indexed paths not in ``alive_paths``. Returns deleted count."""
        existing = await self.list_paths()
        missing = existing - alive_paths
        for path in missing:
            await self.delete_path(path)
        return len(missing)

    async def _delete_chunks_for_path(self, path: str) -> None:
        conn = self._require()
        await conn.execute(
            "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE path = ?)",
            (path,),
        )
        await conn.execute("DELETE FROM chunks WHERE path = ?", (path,))

    async def fts_search(
        self,
        query: str,
        *,
        limit: int = 20,
        path_prefix: str | None = None,
        source: SourceType | None = None,
    ) -> list[dict[str, Any]]:
        """Keyword search via FTS5. Returns chunk rows ordered by rank."""
        conn = self._require()
        fts_query = _to_fts_query(query)
        if not fts_query:
            return []

        clauses = ["chunks_fts MATCH ?"]
        params: list[Any] = [fts_query]
        if path_prefix:
            clauses.append("chunks_fts.path LIKE ?")
            params.append(f"{path_prefix}%")
        if source:
            clauses.append("chunks_fts.source_type = ?")
            params.append(source)
        params.append(max(1, limit))

        sql = f"""
            SELECT chunk_id, path, source_type, text,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            WHERE {' AND '.join(clauses)}
            ORDER BY rank
            LIMIT ?
        """
        try:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            logger.debug("FTS query failed (%s); query=%r", exc, fts_query)
            return []

        out: list[dict[str, Any]] = []
        for row in rows:
            chunk_id = str(row["chunk_id"])
            span = _span_from_chunk_id(chunk_id)
            out.append(
                {
                    "chunk_id": chunk_id,
                    "path": str(row["path"]),
                    "source_type": str(row["source_type"]),
                    "text": str(row["text"]),
                    "rank": float(row["rank"]),
                    "span": span,
                }
            )
        return out

    async def links_from(self, source_path: str) -> list[dict[str, Any]]:
        conn = self._require()
        cur = await conn.execute(
            """
            SELECT target_path, link_type, start_line, end_line
            FROM links WHERE source_path = ?
            """,
            (source_path,),
        )
        rows = await cur.fetchall()
        return [
            {
                "target_path": str(r["target_path"]),
                "link_type": str(r["link_type"]),
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "ref": format_link_ref(
                    ParsedLink(
                        target_path=str(r["target_path"]),
                        link_type=cast(LinkType, str(r["link_type"])),
                        start_line=r["start_line"],
                        end_line=r["end_line"],
                    )
                ),
            }
            for r in rows
        ]

    async def get_chunk_snippet(
        self,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any] | None:
        """Return a chunk (or best overlapping chunk) for a path/span."""
        conn = self._require()
        if start_line is None:
            cur = await conn.execute(
                """
                SELECT id, path, source_type, start_line, end_line, text
                FROM chunks WHERE path = ? ORDER BY start_line LIMIT 1
                """,
                (path,),
            )
        else:
            end = end_line if end_line is not None else start_line
            cur = await conn.execute(
                """
                SELECT id, path, source_type, start_line, end_line, text
                FROM chunks
                WHERE path = ?
                  AND start_line <= ?
                  AND end_line >= ?
                ORDER BY start_line
                LIMIT 1
                """,
                (path, end, start_line),
            )
        row = await cur.fetchone()
        if row is None and start_line is not None:
            # Fall back to any chunk for the path
            cur = await conn.execute(
                """
                SELECT id, path, source_type, start_line, end_line, text
                FROM chunks WHERE path = ? ORDER BY start_line LIMIT 1
                """,
                (path,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "chunk_id": str(row["id"]),
            "path": str(row["path"]),
            "source_type": str(row["source_type"]),
            "start_line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "text": str(row["text"]),
        }


_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+")

# Dropped from MATCH so natural-language questions don't AND into zero hits.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "by",
        "from",
        "with",
        "into",
        "about",
        "over",
        "after",
        "before",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "also",
        "while",
        "during",
        "still",
        "need",
        "needs",
        "using",
        "use",
        "used",
        "via",
        "vs",
        "versus",
        "across",
        "through",
        "against",
        "among",
        "within",
        "without",
        "every",
        "question",
        "questions",
        "please",
        "first",
        "tool",
    }
)

# Prefer longer / identifier-like tokens when truncating.
_MAX_FTS_TOKENS = 8


def _split_tokens(query: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(query) if len(t) >= 2]


def _content_tokens(query: str) -> list[str]:
    """Tokenize a user query for FTS, dropping stopwords and short noise."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _split_tokens(query):
        safe = tok.replace('"', "").strip(".-_/")
        if not safe or len(safe) < 2:
            continue
        key = safe.lower()
        if key in _STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(safe)
    # Prefer identifiers / longer tokens (more selective for OR queries)
    out.sort(key=lambda t: (0 if _looks_identifier(t) else 1, -len(t), t.lower()))
    return out[:_MAX_FTS_TOKENS]


def _looks_identifier(tok: str) -> bool:
    if "/" in tok or "." in tok or "_" in tok:
        return True
    # camelCase or PascalCase
    return any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok)


def _to_fts_query(query: str) -> str:
    """Convert free text into a loose FTS5 MATCH expression (OR of keywords).

    Natural-language questions used to become ``A AND B AND C …`` and often
    matched nothing. OR lets BM25 rank chunks that share more meaningful terms.
    """
    tokens = _content_tokens(query)
    if not tokens:
        # Fall back to raw tokens if everything was a stopword
        tokens = [t.replace('"', "") for t in _split_tokens(query)[:_MAX_FTS_TOKENS] if t]
    if not tokens:
        return ""
    parts = [f'"{tok}"*' for tok in tokens if tok]
    return " OR ".join(parts)


def _span_from_chunk_id(chunk_id: str) -> dict[str, int] | None:
    # path#Lstart-end
    if "#L" not in chunk_id:
        return None
    _, span = chunk_id.rsplit("#L", 1)
    if "-" not in span:
        return None
    try:
        start_s, end_s = span.split("-", 1)
        return {"start_line": int(start_s), "end_line": int(end_s)}
    except ValueError:
        return None


__all__ = ["KnowledgeIndex", "_to_fts_query"]
