from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import ulid

if TYPE_CHECKING:
    from monkeybot.core.provider import Message

try:
    from monkeybot.core.provider import Message
except ImportError:
    from dataclasses import dataclass as _dc

    @_dc
    class Message:  # type: ignore[no-redef]
        role: str
        content: str
        tool_call_id: str | None = None
        tool_name: str | None = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    tool_call_id TEXT,
    tool_name   TEXT,
    created_at  INTEGER NOT NULL
)"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at)
"""


class ConversationHistory:
    """Async SQLite-backed conversation history store."""

    def __init__(self, db_url: str = "sqlite:///data/monkeybot.db") -> None:
        self._db_path: str = db_url.removeprefix("sqlite:///")

    async def init(self) -> None:
        """Create the DB file, tables, and indexes. Safe to call multiple times."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_CREATE_TABLE)
            await db.execute(_CREATE_INDEX)
            await db.commit()

    async def save(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """Persist a message row with a fresh ULID and current timestamp."""
        msg_id = str(ulid.new())
        created_at = int(time.time() * 1000)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO messages "
                "(id, session_id, role, content, tool_call_id, tool_name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, session_id, role, content, tool_call_id, tool_name, created_at),
            )
            await db.commit()

    async def load(self, session_id: str) -> list[Message]:
        """Return all messages for *session_id* sorted by creation time ascending."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT role, content, tool_call_id, tool_name FROM messages "
                "WHERE session_id=? ORDER BY created_at ASC",
                (session_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [
            Message(role=row[0], content=row[1], tool_call_id=row[2], tool_name=row[3])
            for row in rows
        ]

    async def clear(self, session_id: str) -> None:
        """Delete all messages for *session_id*."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            await db.commit()
