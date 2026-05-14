"""SQLite-backed conversation history store."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal, cast

import aiosqlite

Role = Literal["user", "assistant", "tool"]

_VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool"})


@dataclass(frozen=True)
class ChatMessage:
    """Single persisted chat row."""

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    created_at_ms: int = 0


class ConversationHistory:
    """Append/read conversation rows keyed by ``thread_id``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    def _validate_message(self, message: ChatMessage) -> None:
        if message.role not in _VALID_ROLES:
            raise ValueError(f"Invalid message role: {message.role!r}")

    async def append(self, thread_id: str, message: ChatMessage) -> None:
        """Insert a history row with server-side ``created_at`` timestamp."""
        self._validate_message(message)
        created_at = int(time.time() * 1000)
        await self._conn.execute(
            """
            INSERT INTO conversation_history(thread_id, role, content, tool_call_id, tool_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                message.role,
                message.content,
                message.tool_call_id,
                message.tool_name,
                created_at,
            ),
        )
        await self._conn.commit()

    async def load(self, thread_id: str, limit: int = 100) -> list[ChatMessage]:
        """Return up to ``limit`` most recent messages in chronological order."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        cursor = await self._conn.execute(
            """
            SELECT role, content, tool_call_id, tool_name, created_at
            FROM conversation_history
            WHERE thread_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (thread_id, limit),
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
        chronological = list(reversed(rows))
        return [
            ChatMessage(
                role=cast(Role, row[0]),
                content=row[1],
                tool_call_id=row[2],
                tool_name=row[3],
                created_at_ms=int(row[4]),
            )
            for row in chronological
        ]

    async def clear(self, thread_id: str) -> None:
        """Delete all rows for ``thread_id``."""
        await self._conn.execute(
            "DELETE FROM conversation_history WHERE thread_id = ?",
            (thread_id,),
        )
        await self._conn.commit()
