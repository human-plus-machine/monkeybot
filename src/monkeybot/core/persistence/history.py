"""SQLite-backed conversation history store.

Durable conversation facts live here as ``Message`` rows with typed
``ContentBlock`` JSON (not as ``AgentEvent`` rows). Tool settlement is the
``ToolResponse`` block(s) on a user row after a tool batch; the live SSE
``ToolCallResult`` event is the progress mirror (see ``events.is_durable_event``).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, cast

import aiosqlite

from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.persistence.thread_summary import ChatThreadSummary, preview_from_content_blob

logger = logging.getLogger("monkeybot.core.persistence.history")

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")


def _validate_message(message: Message) -> None:
    """Reject persisted shapes that SQLite/provider contracts disallow."""
    role = message.role
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")


class SQLiteHistoryStore:
    """Append/read conversation rows keyed by ``thread_id``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
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
            INSERT INTO conversation_history(thread_id, role, content, created_at, turn_id, message_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (thread_id, message.role, payload, created_at, turn_id, message_id),
        )
        await self._conn.commit()

    async def append_with_outbox(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str,
        message_id: str,
        outbox: dict[str, Any],
    ) -> None:
        """Insert history and a pending memory outbox row in one transaction."""
        from monkeybot.core.memory.outbox import insert_pending

        _validate_message(message)
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute(
                """
                INSERT INTO conversation_history(
                    thread_id, role, content, created_at, turn_id, message_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (thread_id, message.role, payload, created_at, turn_id, message_id),
            )
            await insert_pending(self._conn, commit=False, **outbox)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        """Return messages for ``thread_id``, oldest first.

        When ``limit`` is set, returns the newest ``limit`` rows. When ``None``,
        returns the full thread (compaction owns size — do not silently slide).
        """
        if limit is None:
            cursor = await self._conn.execute(
                """
                SELECT id, role, content, created_at
                FROM conversation_history
                WHERE thread_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (thread_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            rows_chrono = list(rows)
        else:
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

    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        """Return recent threads ordered by last activity (newest first).

        The correlated subquery on ``last_content`` is O(threads × messages) per call.
        For production SQLite load, add a composite index on
        ``conversation_history(thread_id, created_at DESC, id DESC)``.
        """
        cap = max(1, min(limit, 200))
        cursor = await self._conn.execute(
            """
            SELECT
                h.thread_id,
                MAX(h.created_at) AS last_message_at,
                COUNT(*) AS message_count,
                (
                    SELECT h2.content
                    FROM conversation_history h2
                    WHERE h2.thread_id = h.thread_id
                    ORDER BY h2.created_at DESC, h2.id DESC
                    LIMIT 1
                ) AS last_content
            FROM conversation_history h
            GROUP BY h.thread_id
            ORDER BY last_message_at DESC
            LIMIT ?
            """,
            (cap,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        out: list[ChatThreadSummary] = []
        for row in rows:
            thread_id = str(row[0])
            last_at = int(row[1])
            count = int(row[2])
            preview = preview_from_content_blob(str(row[3] or ""))
            out.append(
                ChatThreadSummary(
                    thread_id=thread_id,
                    last_message_at=last_at,
                    message_count=count,
                    preview=preview or "(empty)",
                )
            )
        return out
