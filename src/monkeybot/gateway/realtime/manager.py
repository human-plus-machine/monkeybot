"""Realtime session registry and concurrency guardrails."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.core.logging_utils import kv

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

    def snapshot_metrics(self) -> dict[str, Any]:
        return {
            "active_sessions": len(self._sessions),
            "max_concurrent_sessions": self.config.session.max_concurrent_sessions,
        }
