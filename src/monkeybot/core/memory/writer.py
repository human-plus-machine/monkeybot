"""One lock-coordinated MemPalace writer per agent palace."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

import aiosqlite

from monkeybot.core.memory import outbox as outbox_mod
from monkeybot.core.memory.observability import (
    Timer,
    link_to_traceparent,
    log_event,
    memory_span,
)
from monkeybot.core.memory.palace import PalacePort

logger = logging.getLogger(__name__)


class MemoryWriter:
    """Drains the durable outbox into the per-agent palace."""

    def __init__(
        self,
        *,
        palace: PalacePort,
        db_url: str,
        backend: str,
        embedding_model: str,
        agent_id: str,
    ) -> None:
        self._palace = palace
        self._db_url = db_url
        self._backend = backend
        self._embedding_model = embedding_model
        self._agent_id = agent_id
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._lease_owner = uuid.uuid4().hex
        self._conn: aiosqlite.Connection | None = None
        self._conn_lock = asyncio.Lock()
        self._flushing = asyncio.Event()
        self._flushing.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="memory-writer")
            log_event("writer_start", memory_status="ok", memory_backend=self._backend)

    def wake(self) -> None:
        self._wake.set()

    async def drain(self, *, timeout_s: float = 5.0) -> int:
        """Flush pending rows until empty or timeout. Pending rows survive timeout."""
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_s)
        flushed = 0
        while asyncio.get_running_loop().time() < deadline:
            n = await self.flush_once()
            flushed += n
            if n == 0:
                break
        return flushed

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(self._flushing.wait(), timeout=8.0)
            except TimeoutError:
                logger.warning("memory writer shutdown timed out waiting for in-flight flush")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        await self._close_conn()
        log_event("writer_stop", memory_status="ok", memory_backend=self._backend)

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.flush_once()
            except Exception:
                logger.warning("memory writer flush failed", exc_info=True)
                await self._reset_conn()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except TimeoutError:
                continue
            self._wake.clear()

    async def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            from monkeybot.core.persistence.sqlite import open_connection

            conn = await open_connection(self._db_url)
            await outbox_mod.ensure_outbox_schema(conn)
            self._conn = conn
        return self._conn

    async def _close_conn(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            await conn.close()

    async def _reset_conn(self) -> None:
        try:
            await self._close_conn()
        except Exception:
            logger.debug("memory writer connection reset failed", exc_info=True)
            self._conn = None

    async def flush_once(self) -> int:
        self._flushing.clear()
        try:
            async with self._conn_lock:
                conn = await self._connection()
                rows = await outbox_mod.claim_batch(
                    conn, lease_owner=self._lease_owner, agent_id=self._agent_id
                )
                if not rows:
                    return 0
                log_event(
                    "outbox_claim",
                    memory_status="ok",
                    memory_batch_size=len(rows),
                    memory_backend=self._backend,
                )
                return await self._upsert_batch(conn, rows)
        finally:
            self._flushing.set()

    async def _upsert_batch(
        self, conn: aiosqlite.Connection, rows: list[outbox_mod.OutboxRow]
    ) -> int:
        committed = 0
        for row in rows:
            timer = Timer()
            lock_timer = Timer()
            try:
                with self._palace.acquire_write_lock():
                    wait_ms = lock_timer.ms()
                    with memory_span(
                        "monkeybot.memory.writer.flush",
                        **{
                            "memory.backend": self._backend,
                            "memory.embedding_model": self._embedding_model,
                            "memory.operation": "flush",
                            "memory.batch_size": 1,
                        },
                    ) as span:
                        if row.traceparent:
                            link_to_traceparent(span, row.traceparent)
                        await asyncio.to_thread(self._upsert_rows, [row])
                await outbox_mod.mark_committed(conn, [row.id], lease_owner=self._lease_owner)
                log_event(
                    "writer_commit",
                    memory_status="committed",
                    memory_batch_size=1,
                    memory_backend=self._backend,
                    memory_duration_ms=round(timer.ms(), 1),
                    memory_lock_wait_ms=round(wait_ms, 1),
                )
                committed += 1
            except Exception as exc:
                error_class = type(exc).__name__
                logger.warning("memory writer upsert failed: %r", exc)
                log_event(
                    "writer_retry",
                    memory_status="retry",
                    memory_error_class=error_class,
                    memory_batch_size=1,
                )
                await outbox_mod.mark_retry(
                    conn,
                    row.id,
                    error_class=error_class,
                    attempts=row.attempts + 1,
                    lease_owner=self._lease_owner,
                )
        return committed

    def _upsert_rows(self, rows: list[outbox_mod.OutboxRow]) -> None:
        for row in rows:
            content = row.content or ""
            self._palace.upsert_drawer(row.id, content, row.metadata())
