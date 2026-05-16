"""SQLite-backed conversation history store."""

from __future__ import annotations

import json
import logging
import time
from typing import cast

import aiosqlite

from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.core.llm.provider import Message, Role

logger = logging.getLogger("monkeybot.core.persistence.history")

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")


def _validate_message(message: Message) -> None:
    """Reject persisted shapes that SQLite/provider contracts disallow."""
    role = message.role
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")


class ConversationHistory:
    """Append/read conversation rows keyed by ``thread_id``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append(self, thread_id: str, message: Message) -> None:
        """Validate ``message``, JSON-encode content blocks, insert one row."""
        _validate_message(message)
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        await self._conn.execute(
            """
            INSERT INTO conversation_history(thread_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (thread_id, message.role, payload, created_at),
        )
        await self._conn.commit()

    async def load(self, thread_id: str, limit: int = 100) -> list[Message]:
        """Return up to ``limit`` messages for ``thread_id``, oldest first."""
        cursor = await self._conn.execute(
            """
            SELECT id, role, content, created_at
            FROM conversation_history
            WHERE thread_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (thread_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        rows_chrono = list(reversed(list(rows)))
        out: list[Message] = []
        for row in rows_chrono:
            row_id = int(row[0])
            role = row[1]
            content_blob = row[2]
            try:
                raw = json.loads(content_blob)
                if not isinstance(raw, list):
                    raise ValueError("stored content must be a JSON array")
                blocks = [ContentBlock.from_dict(b) for b in raw]
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.error(
                    "Unparseable history row id=%s thread_id=%s",
                    row_id,
                    thread_id,
                    exc_info=True,
                )
                raise ValueError(f"history row {row_id} unparseable: {exc}") from exc
            if role not in _VALID_ROLES:
                raise ValueError(f"history row {row_id} has invalid role: {role!r}")
            out.append(Message(role=cast(Role, role), content=blocks))
        return out

    async def clear(self, thread_id: str) -> None:
        """Delete every stored message for ``thread_id``."""

        await self._conn.execute(
            "DELETE FROM conversation_history WHERE thread_id = ?", (thread_id,)
        )
        await self._conn.commit()

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        """Replace the thread transcript with ``messages`` (validated like ``append``)."""

        await self._conn.execute(
            "DELETE FROM conversation_history WHERE thread_id = ?", (thread_id,)
        )
        await self._conn.commit()
        for msg in messages:
            await self.append(thread_id, msg)
