"""Realtime session registry and concurrency guardrails."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.core.logging_utils import kv
from monkeybot.todo_list import TodoListStore

from .session import RealtimeConnectionState

logger = logging.getLogger("monkeybot.gateway.realtime.manager")


@dataclass
class RealtimeSessionManager:
    """In-process manager for realtime sessions.

    Mirrors the existing SSE ``SessionRegistry`` pattern: one instance per process.
    Multi-instance deployments require sticky routing or an external pub/sub (out of
    scope for v1, per the realtime design doc).
    """

    config: RealtimeConfig
    _sessions: dict[str, RealtimeConnectionState] = field(default_factory=dict)
    _todo_stores: dict[str, TodoListStore] = field(default_factory=dict)
    _sem: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.config.session.max_concurrent_sessions)

    async def acquire_slot(self, session_id: str) -> bool:
        """Acquire a concurrency slot. Returns True if granted, False if at limit."""
        if self._sem.locked():
            logger.warning(
                "realtime concurrency limit reached %s",
                kv(
                    session_id=session_id,
                    max_concurrent=self.config.session.max_concurrent_sessions,
                ),
            )
            return False
        await self._sem.acquire()
        return True

    def release_slot(self, session_id: str) -> None:
        self._sem.release()
        logger.debug(
            "realtime concurrency slot released %s",
            kv(session_id=session_id),
        )

    def register(self, session_id: str, state: RealtimeConnectionState) -> None:
        """Register a live connection. Raises if ``session_id`` is already active."""
        if session_id in self._sessions:
            raise ValueError(f"Realtime session already active: {session_id}")
        self._sessions[session_id] = state

    def get(self, session_id: str) -> RealtimeConnectionState | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str, state: RealtimeConnectionState | None = None) -> None:
        """Remove a session. When ``state`` is given, only remove that exact connection."""
        current = self._sessions.get(session_id)
        if current is None:
            return
        if state is not None and current is not state:
            return
        self._sessions.pop(session_id, None)
        # Drop the cached todo store so long-running gateways don't retain an
        # entry for every session_id ever seen. A reconnect on the same id
        # rehydrates from todos.json when disk mirroring is enabled.
        self._todo_stores.pop(session_id, None)

    def snapshot_metrics(self) -> dict[str, Any]:
        return {
            "active_sessions": len(self._sessions),
            "max_concurrent_sessions": self.config.session.max_concurrent_sessions,
        }

    async def get_or_create_todo_store(
        self, session_id: str, *, workspace_root: Path
    ) -> TodoListStore:
        """Return the process-cached todo list for ``session_id``, creating it if absent.

        Cached on the manager (not per-connection) while the session is live so
        mid-session lookups reuse the same store. ``remove()`` evicts the entry;
        a later reconnect constructs a new store and rehydrates from ``todos.json``
        (via ``asyncio.to_thread``) when ``todo_list.mirror_to_disk`` is enabled.
        """
        store = self._todo_stores.get(session_id)
        if store is None:
            store = TodoListStore(session_id, workspace_root=workspace_root)
            await store.hydrate_from_disk()
            self._todo_stores[session_id] = store
        return store
