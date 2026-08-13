"""One lock-coordinated MemPalace writer per agent palace."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid

from monkeybot.core.memory.observability import (
    Timer,
    link_to_traceparent,
    log_event,
    memory_span,
)
from monkeybot.core.memory.outbox import OutboxRow, OutboxStore, is_permanent_error
from monkeybot.core.memory.palace import PalacePort

logger = logging.getLogger(__name__)

_STOP_TIMEOUT_S = 10.0


class MemoryWriter:
    """Drains the durable outbox into the per-agent palace."""

    def __init__(
        self,
        *,
        palace: PalacePort,
        outbox: OutboxStore,
        agent_id: str,
        backend: str,
        embedding_model: str,
        palace_id: str = "",
    ) -> None:
        self._palace = palace
        self._outbox = outbox
        self._agent_id = agent_id
        self._backend = backend
        self._embedding_model = embedding_model
        self._palace_id = palace_id
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._lease_owner = uuid.uuid4().hex
        self._flush_lock = asyncio.Lock()
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()

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

    async def stop(self, *, timeout_s: float = _STOP_TIMEOUT_S) -> None:
        """Finish the in-flight write if it completes within ``timeout_s``.

        A hung embedder must not block gateway shutdown forever. After the
        timeout the writer task is abandoned; the worker thread may still run.
        """
        self._stopped = True
        self._wake.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, timeout_s))
            except TimeoutError:
                logger.warning(
                    "memory writer stop timed out after %.1fs; abandoning in-flight write",
                    timeout_s,
                )
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, timeout=0.2)
        self._task = None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout=0.2)
        log_event("writer_stop", memory_status="ok", memory_backend=self._backend)

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.flush_once()
            except Exception:
                logger.warning("memory writer flush failed", exc_info=True)
            if self._stopped:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except TimeoutError:
                continue
            self._wake.clear()

    async def flush_once(self) -> int:
        async with self._flush_lock:
            self._in_flight += 1
            self._idle.clear()
            try:
                rows = await self._outbox.claim_batch(
                    agent_id=self._agent_id,
                    lease_owner=self._lease_owner,
                    palace_id=self._palace_id,
                )
                if not rows:
                    return 0
                log_event(
                    "outbox_claim",
                    memory_status="ok",
                    memory_batch_size=len(rows),
                    memory_backend=self._backend,
                )
                return await self._commit_rows(rows)
            finally:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._idle.set()

    async def _commit_rows(self, rows: list[OutboxRow]) -> int:
        timer = Timer()
        committed = 0
        with memory_span(
            "monkeybot.memory.writer.flush",
            **{
                "memory.backend": self._backend,
                "memory.embedding_model": self._embedding_model,
                "memory.operation": "flush",
                "memory.batch_size": len(rows),
            },
        ) as span:
            if rows[0].traceparent:
                link_to_traceparent(span, rows[0].traceparent)
            for row in rows:
                try:
                    await self._upsert_async(row)
                    await self._outbox.mark_committed(
                        [row.id], lease_owner=self._lease_owner
                    )
                    committed += 1
                except Exception as exc:
                    error_class = type(exc).__name__
                    permanent = is_permanent_error(error_class)
                    logger.warning(
                        "memory writer upsert failed id=%s permanent=%s: %r",
                        row.id,
                        permanent,
                        exc,
                    )
                    log_event(
                        "writer_retry" if not permanent else "writer_dead",
                        memory_status="dead" if permanent else "retry",
                        memory_error_class=error_class,
                        memory_batch_size=1,
                    )
                    await self._outbox.mark_retry(
                        row.id,
                        error_class=error_class,
                        attempts=row.attempts + 1,
                        permanent=permanent,
                        lease_owner=self._lease_owner,
                    )
            if committed:
                log_event(
                    "writer_commit",
                    memory_status="committed",
                    memory_batch_size=committed,
                    memory_backend=self._backend,
                    memory_duration_ms=round(timer.ms(), 1),
                )
            dead = await self._outbox.dead_depth(agent_id=self._agent_id)
            if dead:
                log_event(
                    "outbox_dead_depth",
                    memory_status="dead",
                    memory_batch_size=dead,
                    memory_backend=self._backend,
                )
        return committed

    async def _upsert_async(self, row: OutboxRow) -> None:
        """Run the palace upsert on a daemon thread so process exit is not blocked.

        ``asyncio.to_thread`` uses the default executor; Python joins those
        threads at shutdown. A hung embedder must not keep the process alive
        past the writer stop timeout.
        """
        error: list[BaseException] = []
        done = threading.Event()

        def run() -> None:
            try:
                self._upsert_one(row)
            except BaseException as exc:
                error.append(exc)
            finally:
                done.set()

        threading.Thread(
            target=run, name="memory-palace-upsert", daemon=True
        ).start()
        while not done.is_set():
            await asyncio.sleep(0.05)
        if error:
            raise error[0]

    def _upsert_one(self, row: OutboxRow) -> None:
        """Lock ownership lives in this worker thread for the duration of the upsert."""
        with self._palace.acquire_write_lock():
            content = row.content or ""
            self._palace.upsert_drawer(row.id, content, row.metadata())
