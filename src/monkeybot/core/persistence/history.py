"""SQLite-backed conversation history store.

Durable conversation facts live here as ``Message`` rows with typed
``ContentBlock`` JSON (not as ``AgentEvent`` rows). Tool settlement is the
``ToolResponse`` block(s) on a user row after a tool batch; the live SSE
``ToolCallResult`` event is the progress mirror (see ``events.is_durable_event``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Collection
from typing import Any, cast

import aiosqlite

from monkeybot.core.llm.provider import Message, Role
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.row_ids import loaded_row_id, row_id_for_insert, split_row_ids
from monkeybot.core.persistence.sqlite import ConnLock, with_conn_lock
from monkeybot.core.persistence.thread_summary import (
    BRANCH_THREAD_ID_PREFIX,
    SUBAGENT_THREAD_ID_PREFIX,
    ChatThreadSummary,
    preview_from_content_blob,
)
from monkeybot.core.types.content_blocks import ContentBlock

logger = logging.getLogger("monkeybot.core.persistence.history")

_VALID_ROLES: tuple[str, ...] = ("user", "assistant", "system")
# Stays under SQLITE_MAX_VARIABLE_NUMBER (999 on older builds) with the scope params.
_DELETE_CHUNK = 500


def _validate_message(message: Message) -> None:
    """Reject persisted shapes that SQLite/provider contracts disallow."""
    role = message.role
    if role not in _VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")


class SQLiteHistoryStore:
    """Append/read conversation rows keyed by ``thread_id``, scoped to ``agent_scope``.

    ``agent_scope`` isolates threads when one DB_URL is shared across gateways
    for different agent roots (e.g. a shared Postgres/Firestore backend) — without
    it, ``list_threads`` would surface another agent's newest transcript. Defaults
    to ``''`` (unscoped) for in-process/test callers that only ever see their own
    rows; production gateways pass the resolved agent root.
    """

    def __init__(
        self,
        conn: aiosqlite.Connection,
        agent_scope: str = "",
        *,
        lock: ConnLock | None = None,
    ) -> None:
        self._conn = conn
        self._agent_scope = agent_scope
        self._lock = lock or asyncio.Lock()
        self._column_names: frozenset[str] | None = None

    async def _columns(self) -> frozenset[str]:
        if self._column_names is None:
            cur = await self._conn.execute("PRAGMA table_info(conversation_history)")
            rows = await cur.fetchall()
            await cur.close()
            self._column_names = frozenset(str(r[1]) for r in rows)
        return self._column_names

    async def _has_memory_columns(self) -> bool:
        columns = await self._columns()
        return "turn_id" in columns and "message_id" in columns

    async def _insert_history_row(
        self,
        thread_id: str,
        role: str,
        payload: str,
        created_at: int,
        *,
        row_id: str,
        turn_id: str | None,
        message_id: str | None,
    ) -> None:
        names = ["thread_id", "role", "content", "created_at", "agent_scope"]
        values: list[object] = [thread_id, role, payload, created_at, self._agent_scope]
        if "row_id" in await self._columns():
            names.append("row_id")
            values.append(row_id)
        memory = await self._has_memory_columns()
        if memory:
            if message_id:
                cur = await self._conn.execute(
                    """
                    SELECT 1 FROM conversation_history
                    WHERE message_id = ? LIMIT 1
                    """,
                    (message_id,),
                )
                exists = await cur.fetchone()
                await cur.close()
                if exists is not None:
                    return
            names.extend(("turn_id", "message_id"))
            values.extend((turn_id, message_id))
        placeholders = ", ".join("?" for _ in names)
        try:
            await self._conn.execute(
                f"INSERT INTO conversation_history({', '.join(names)}) VALUES ({placeholders})",
                tuple(values),
            )
        except aiosqlite.IntegrityError:
            if not memory:
                raise

    async def append(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Validate ``message``, JSON-encode content blocks, insert one row."""
        async with self._lock:
            await self._insert_message(
                thread_id,
                message,
                turn_id=turn_id,
                message_id=message_id,
            )
            await self._conn.commit()

    async def _insert_message(
        self,
        thread_id: str,
        message: Message,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        """Insert one history row without committing (caller owns the transaction)."""
        _validate_message(message)
        payload = json.dumps(
            [b.to_dict() for b in message.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        created_at = int(time.time() * 1000)
        await self._insert_history_row(
            thread_id,
            message.role,
            payload,
            created_at,
            row_id=row_id_for_insert(message),
            turn_id=turn_id,
            message_id=message_id,
        )

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
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                if not await self._has_memory_columns():
                    raise RuntimeError(
                        "conversation_history is missing turn_id/message_id. "
                        "Apply docs/migrations/memory-outbox.sql or set paths.auto_schema: true"
                    )
                await self._insert_history_row(
                    thread_id,
                    message.role,
                    payload,
                    created_at,
                    row_id=row_id_for_insert(message),
                    turn_id=turn_id,
                    message_id=message_id,
                )
                await insert_pending(self._conn, commit=False, **outbox)
                await self._conn.commit()
            except Exception as exc:
                await self._conn.rollback()
                if isinstance(
                    exc, (TimeoutError, OSError, ConnectionError, aiosqlite.OperationalError)
                ):
                    from monkeybot.core.persistence.errors import AmbiguousCommitError

                    raise AmbiguousCommitError(str(exc)) from exc
                raise

    @with_conn_lock
    async def load(self, thread_id: str, limit: int | None = None) -> list[Message]:
        """Return messages for ``thread_id`` within this store's agent scope, oldest first.

        When ``limit`` is set, returns the newest ``limit`` rows. When ``None``,
        returns the full thread (compaction owns size — do not silently slide).
        """
        row_id_column = "row_id" if "row_id" in await self._columns() else "NULL"
        if limit is None:
            cursor = await self._conn.execute(
                f"""
                SELECT id, role, content, created_at, {row_id_column}
                FROM conversation_history
                WHERE thread_id = ? AND agent_scope = ?
                ORDER BY created_at ASC, id ASC
                """,
                (thread_id, self._agent_scope),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            rows_chrono = list(rows)
        else:
            cursor = await self._conn.execute(
                f"""
                SELECT id, role, content, created_at, {row_id_column}
                FROM conversation_history
                WHERE thread_id = ? AND agent_scope = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (thread_id, self._agent_scope, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            rows_chrono = list(reversed(list(rows)))
        out: list[Message] = []
        for row in rows_chrono:
            db_id = int(row[0])
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
                    db_id,
                    thread_id,
                    exc_info=True,
                )
                raise ValueError(f"history row {db_id} unparseable: {exc}") from exc
            if role not in _VALID_ROLES:
                raise ValueError(f"history row {db_id} has invalid role: {role!r}")
            out.append(
                Message(
                    role=cast(Role, role),
                    content=blocks,
                    row_id=loaded_row_id(row[4], db_id),
                )
            )
        return out

    @with_conn_lock
    async def last_row(self, thread_id: str) -> tuple[int, str] | None:
        """Message count and the newest row's content JSON, without loading the thread."""
        cursor = await self._conn.execute(
            """
            SELECT
                COUNT(*),
                (
                    SELECT h2.content
                    FROM conversation_history h2
                    WHERE h2.thread_id = ? AND h2.agent_scope = ?
                    ORDER BY h2.created_at DESC, h2.id DESC
                    LIMIT 1
                )
            FROM conversation_history h
            WHERE h.thread_id = ? AND h.agent_scope = ?
            """,
            (thread_id, self._agent_scope, thread_id, self._agent_scope),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or not row[0]:
            return None
        return int(row[0]), str(row[1] or "")

    async def clear(self, thread_id: str) -> None:
        """Delete every stored message for ``thread_id`` within this store's agent scope."""
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM conversation_history WHERE thread_id = ? AND agent_scope = ?",
                (thread_id, self._agent_scope),
            )
            await self._conn.commit()

    @with_conn_lock
    async def delete_rows(self, thread_id: str, row_ids: Collection[str]) -> int:
        """Delete the rows whose loaded ``row_id`` is in ``row_ids`` in one transaction."""
        stored, legacy_keys = split_row_ids(row_ids)
        legacy_ids = [int(key) for key in legacy_keys if key.isdigit()]
        has_row_id = "row_id" in await self._columns()
        scope = "thread_id = ? AND agent_scope = ?"
        deleted = 0
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            if has_row_id:
                for start in range(0, len(stored), _DELETE_CHUNK):
                    chunk = stored[start : start + _DELETE_CHUNK]
                    cursor = await self._conn.execute(
                        f"DELETE FROM conversation_history WHERE {scope} "
                        f"AND row_id IN ({','.join('?' * len(chunk))})",
                        (thread_id, self._agent_scope, *chunk),
                    )
                    deleted += cursor.rowcount
            unstored = "(row_id IS NULL OR row_id = '')" if has_row_id else "1"
            for start in range(0, len(legacy_ids), _DELETE_CHUNK):
                id_chunk = legacy_ids[start : start + _DELETE_CHUNK]
                cursor = await self._conn.execute(
                    f"DELETE FROM conversation_history WHERE {scope} AND {unstored} "
                    f"AND id IN ({','.join('?' * len(id_chunk))})",
                    (thread_id, self._agent_scope, *id_chunk),
                )
                deleted += cursor.rowcount
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
        return deleted

    async def reset(self, thread_id: str, messages: list[Message]) -> None:
        """Replace the thread transcript with ``messages`` (validated like ``append``).

        Delete + re-insert run in a single SQLite transaction so a crash mid-reset
        cannot leave the thread empty or only partially rewritten.
        """
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(
                    "DELETE FROM conversation_history WHERE thread_id = ? AND agent_scope = ?",
                    (thread_id, self._agent_scope),
                )
                for msg in messages:
                    await self._insert_message(thread_id, msg)
                await self._conn.commit()
            except Exception as exc:
                await self._conn.rollback()
                logger.warning(
                    "history reset rolled back %s",
                    kv(thread_id=thread_id),
                    exc_info=True,
                )
                if isinstance(
                    exc,
                    (TimeoutError, OSError, ConnectionError, aiosqlite.OperationalError),
                ):
                    from monkeybot.core.persistence.errors import AmbiguousCommitError

                    raise AmbiguousCommitError(str(exc)) from exc
                raise

    @with_conn_lock
    async def list_threads(self, limit: int = 50) -> list[ChatThreadSummary]:
        """Return recent threads in this store's agent scope, ordered by last activity (newest first).

        Excludes subagent transcripts (``thread_id`` prefixed
        ``SUBAGENT_THREAD_ID_PREFIX``) and conversation branches
        (``BRANCH_THREAD_ID_PREFIX``) — otherwise one that finishes after its
        parent's last turn would outrank the parent as "newest," making
        ``--continue`` resume the wrong transcript.
        Uses ``GLOB``, not ``LIKE``: SQLite's ``LIKE`` ASCII-folds case by
        default, so ``NOT LIKE 'subagent:%'`` would also swallow an ordinary
        user session literally named e.g. ``Subagent:foo`` even though the
        internal prefix (and the reserved-namespace rejection at session
        creation) is exact-case — ``GLOB`` matches case-sensitively, like
        Postgres's ``LIKE`` and Python's ``str.startswith`` already do.

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
                    WHERE h2.thread_id = h.thread_id AND h2.agent_scope = h.agent_scope
                    ORDER BY h2.created_at DESC, h2.id DESC
                    LIMIT 1
                ) AS last_content
            FROM conversation_history h
            WHERE h.agent_scope = ?
              AND h.thread_id NOT GLOB ? || '*'
              AND h.thread_id NOT GLOB ? || '*'
            GROUP BY h.thread_id
            ORDER BY last_message_at DESC
            LIMIT ?
            """,
            (self._agent_scope, SUBAGENT_THREAD_ID_PREFIX, BRANCH_THREAD_ID_PREFIX, cap),
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
