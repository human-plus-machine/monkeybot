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
from monkeybot.core.memory.outbox import OutboxStore, SqliteOutboxStore
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


def _share_across_threads() -> bool:
    raw = os.environ.get("MONKEYBOT_MEMORY_SHARE_THREADS", "").strip().lower()
    return raw in ("1", "true", "yes")


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
        storage: Any | None = None,
        outbox: OutboxStore | None = None,
    ) -> None:
        self._memory_uri = memory_uri.strip()
        self._db_url = db_url
        self._agent_id = agent_id
        self._storage = storage
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
        self._writer_enabled = writer_enabled
        self._outbox: OutboxStore | None = outbox
        self._owned_outbox = False
        self._writer: MemoryWriter | None = None
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

    async def _ensure_outbox(self) -> OutboxStore:
        if self._outbox is not None:
            return self._outbox
        if self._storage is not None:
            self._outbox = self._storage.outbox()
            return self._outbox
        if not self._db_url.startswith("sqlite:"):
            raise RuntimeError(
                "Memory outbox requires a StorageBackend for non-SQLite DB_URL "
                f"({self._db_url.split(':', 1)[0]}). Pass storage= when constructing "
                "MemorySubsystem, or use sqlite:///."
            )
        if ":memory:" in self._db_url:
            raise RuntimeError(
                "sqlite:///:memory: outbox requires a shared StorageBackend or OutboxStore; "
                "separate connections are distinct databases"
            )
        from monkeybot.core.memory.outbox import ensure_outbox_schema
        from monkeybot.core.persistence.sqlite import open_connection

        conn = await open_connection(self._db_url)
        await ensure_outbox_schema(conn)
        self._outbox = SqliteOutboxStore(conn, owns_connection=True)
        self._owned_outbox = True
        return self._outbox

    def _ensure_writer(self, store: OutboxStore) -> None:
        if not self._writer_enabled or self._writer is not None:
            return
        self._writer = MemoryWriter(
            palace=self._palace,
            outbox=store,
            agent_id=self._agent_id,
            backend=self.backend,
            embedding_model=self.embedding_model,
        )

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        store = await self._ensure_outbox()
        try:
            n = await store.gc_committed()
            if n:
                log_event("outbox_gc", memory_status="ok", memory_batch_size=n)
            dead = await store.dead_depth(agent_id=self._agent_id)
            if dead:
                log_event(
                    "outbox_dead_depth",
                    memory_status="dead",
                    memory_batch_size=dead,
                )
        except Exception as exc:
            logger.warning("memory outbox gc failed: %r", exc)
        try:
            await asyncio.to_thread(self._palace.ensure_ready)
        except Exception as exc:
            logger.warning("memory palace warm failed: %r", exc)
            log_event(
                "embedder_warm",
                memory_status="error",
                memory_error_class=type(exc).__name__,
            )
        try:
            from monkeybot.core.memory.import_notes import import_legacy_notes

            imported = await asyncio.to_thread(
                import_legacy_notes, self._palace, agent_id=self._agent_id
            )
            if imported:
                log_event(
                    "legacy_notes_imported",
                    memory_status="ok",
                    memory_batch_size=imported,
                )
        except Exception as exc:
            logger.warning("legacy note import failed: %r", exc)
        self._ensure_writer(store)
        if self._writer is not None:
            self._writer.start()
        self._ready = True

    async def load_index(self, *, thread_id: str | None = None) -> list[str]:
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
                text = await asyncio.to_thread(
                    self._palace.wake_up,
                    wing,
                    None if _share_across_threads() else thread_id,
                )
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
        scoped = None if _share_across_threads() else thread_id
        return await asyncio.to_thread(
            self._palace.recall,
            wing=wing,
            room=room,
            n_results=10,
            thread_id=scoped,
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
        spec = self.outbox_spec(
            thread_id=thread_id,
            turn_id=turn_id,
            message_id=message_id,
            role=role,
            content=content,
            traceparent=traceparent,
        )
        store = await self._ensure_outbox()
        self._ensure_writer(store)
        return await store.insert_pending(**spec)

    def wake_writer(self) -> None:
        if self._writer is not None:
            self._writer.wake()

    async def drain_writer(self, *, timeout_s: float = 5.0) -> int:
        store = await self._ensure_outbox()
        self._ensure_writer(store)
        if self._writer is None:
            return 0
        return await self._writer.drain(timeout_s=timeout_s)

    async def flush(self) -> None:
        await self.drain_writer()

    async def close(self) -> None:
        if self._writer is not None:
            await self._writer.stop()
            self._writer = None
        if self._owned_outbox and self._outbox is not None:
            await self._outbox.close()
            self._outbox = None
            self._owned_outbox = False
        self._ready = False

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

    async def export_graph(self, *, refresh: bool = False) -> dict[str, Any]:
        del refresh
        status = await asyncio.to_thread(self._palace.status)
        return {
            "nodes": [],
            "edges": [],
            "deprecated": True,
            "engine": "mempalace",
            "note": "Note-graph export was replaced by MemPalace drawers.",
            "palace": status,
        }

    async def search_files(
        self,
        query: str,
        *,
        top_k: int = 5,
        path: str | None = None,
    ) -> dict[str, Any]:
        del query, top_k
        if path:
            record = await self.get_drawer(path)
            if record is None:
                return {"hits": [], "deprecated": True}
            return {
                "hits": [
                    {
                        "path": record["id"],
                        "snippet": record["content"][:500],
                        "type": record.get("room"),
                    }
                ],
                "deprecated": True,
            }
        return {"hits": [], "deprecated": True, "note": "Use mempalace search via run_command."}
