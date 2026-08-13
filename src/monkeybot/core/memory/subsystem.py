"""MemPalace memory subsystem: wake-up, L2 recall, and outbox writer."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from monkeybot.core.hooks import HookManager
from monkeybot.core.memory.hook import MemoryHook
from monkeybot.core.memory.ids import conversation_wing, utc_now_iso
from monkeybot.core.memory.ingest import workspace_id_from_env
from monkeybot.core.memory.observability import Timer, log_event, memory_span
from monkeybot.core.memory.outbox import (
    ensure_outbox_schema,
    gc_committed,
    insert_pending,
)
from monkeybot.core.memory.palace import (
    CONVERSATION_ROOM,
    DEFAULT_BACKEND,
    DEFAULT_EMBEDDING_MODEL,
    DrawerRecord,
    PalacePort,
    create_palace,
)
from monkeybot.core.memory.writer import MemoryWriter

logger = logging.getLogger(__name__)


class MemorySubsystem:
    """Owns the per-agent MemPalace root, durable outbox, and writer."""

    def __init__(
        self,
        *,
        memory_uri: str,
        db_url: str,
        agent_id: str,
        agent_name: str = "",
        palace: PalacePort | None = None,
        ingest_enabled: bool = True,
        writer_enabled: bool = True,
        backend: str | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self._memory_uri = memory_uri.strip()
        self._db_url = db_url
        self._agent_id = agent_id
        self.backend = (backend or os.environ.get("MEMPALACE_BACKEND") or DEFAULT_BACKEND).strip()
        self.embedding_model = (
            embedding_model
            or os.environ.get("MEMPALACE_EMBEDDING_MODEL")
            or DEFAULT_EMBEDDING_MODEL
        ).strip()
        self._palace: PalacePort = palace or create_palace(
            self._memory_uri,
            agent_name=agent_name or agent_id,
            backend=self.backend,
            embedding_model=self.embedding_model,
        )
        self.backend = getattr(self._palace, "backend", self.backend)
        self.ingest_enabled = ingest_enabled
        self._writer = (
            MemoryWriter(
                palace=self._palace,
                db_url=db_url,
                backend=self.backend,
                embedding_model=self.embedding_model,
                agent_id=agent_id,
            )
            if writer_enabled
            else None
        )
        self._hook = MemoryHook(self)
        self._ready = False

    @property
    def uri(self) -> str:
        return self._memory_uri

    @property
    def palace_path(self) -> Path:
        return self._palace.palace_path

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def register_hooks(self, manager: HookManager) -> None:
        self._hook.register(manager)

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        await self._ensure_schema_and_gc()
        try:
            await asyncio.to_thread(self._palace.ensure_ready)
        except Exception as exc:
            logger.warning("memory palace warm failed: %r", exc)
            log_event(
                "embedder_warm",
                memory_status="error",
                memory_error_class=type(exc).__name__,
            )
        if self._writer is not None:
            self._writer.start()
        self._ready = True

    async def _ensure_schema_and_gc(self) -> None:
        from monkeybot.core.persistence.sqlite import open_connection

        conn = await open_connection(self._db_url)
        try:
            await ensure_outbox_schema(conn)
            n = await gc_committed(conn)
            if n:
                log_event("outbox_gc", memory_status="ok", memory_batch_size=n)
        finally:
            await conn.close()

    async def load_index(self) -> list[str]:
        """L0+L1 wake-up lines for the system prompt."""
        timer = Timer()
        wing = conversation_wing(workspace_id_from_env())
        with memory_span(
            "monkeybot.memory.wake_up",
            **{
                "memory.operation": "wake_up",
                "memory.wing": wing,
                "memory.backend": self.backend,
                "memory.embedding_model": self.embedding_model,
            },
        ):
            try:
                text = await asyncio.to_thread(self._palace.wake_up, wing)
            except Exception as exc:
                logger.warning("memory wake-up failed: %r", exc)
                return []
        lines = [ln for ln in text.splitlines() if ln.strip()]
        log_event(
            "wake_up",
            memory_status="ok",
            memory_backend=self.backend,
            memory_result_count=len(lines),
            memory_duration_ms=round(timer.ms(), 1),
        )
        try:
            status = self._palace.status()
            log_event(
                "palace_status",
                memory_status="ok",
                memory_backend=self.backend,
                memory_drawer_count=status.get("total_drawers") or 0,
            )
        except Exception as exc:
            logger.warning("memory palace status failed: %r", exc)
        return lines

    async def recall(
        self,
        *,
        wing: str,
        room: str = CONVERSATION_ROOM,
        thread_id: str | None = None,
    ) -> list[DrawerRecord]:
        return await asyncio.to_thread(
            self._palace.recall, wing=wing, room=room, n_results=10, thread_id=thread_id
        )

    def outbox_spec(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message_id: str,
        role: str,
        content: str,
        traceparent: str | None,
    ) -> dict[str, Any]:
        ws = workspace_id_from_env()
        return {
            "agent_id": self._agent_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "message_id": message_id,
            "role": role,
            "content": content,
            "workspace_id": ws,
            "wing": conversation_wing(ws),
            "room": CONVERSATION_ROOM,
            "created_at": utc_now_iso(),
            "traceparent": traceparent,
        }

    async def enqueue(
        self,
        *,
        thread_id: str,
        turn_id: str,
        message_id: str,
        role: str,
        content: str,
        traceparent: str | None = None,
    ) -> str | None:
        from monkeybot.core.persistence.sqlite import open_connection

        spec = self.outbox_spec(
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=message_id,
            role=role,
            content=content,
            traceparent=traceparent,
        )
        conn = await open_connection(self._db_url)
        try:
            await ensure_outbox_schema(conn)
            return await insert_pending(conn, **spec)
        finally:
            await conn.close()

    def wake_writer(self) -> None:
        if self._writer is not None:
            self._writer.wake()

    async def drain_writer(self, *, timeout_s: float = 5.0) -> int:
        if self._writer is None:
            return 0
        return await self._writer.drain(timeout_s=timeout_s)

    async def flush(self) -> None:
        await self.drain_writer()

    async def close(self) -> None:
        if self._writer is not None:
            await self._writer.stop()

    async def get_drawer(self, drawer_id: str) -> dict[str, Any] | None:
        with memory_span(
            "monkeybot.memory.drawer.query",
            **{"memory.operation": "drawer.query", "memory.backend": self.backend},
        ):
            record = await asyncio.to_thread(self._palace.get_drawer, drawer_id)
        if record is None:
            return None
        return {
            "id": record.drawer_id,
            "content": record.content,
            "wing": record.wing,
            "room": record.room,
            "filed_at": record.filed_at,
            "metadata": record.metadata,
        }
