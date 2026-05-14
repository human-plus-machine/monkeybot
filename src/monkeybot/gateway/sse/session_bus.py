"""
In-memory per-session SSE bus with replay buffer and live subscribers.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque

from .sse import format_data_event


def _replay_maxlen_from_env() -> int:
    raw = os.environ.get("SSE_REPLAY_MAX", "256")
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        return 256


class SessionAlreadyExistsError(Exception):
    """Raised when POST /sessions repeats an existing client-supplied id."""


class SessionBus:
    """Broadcasts framed SSE events; buffers numbered data events for replay."""

    def __init__(
        self,
        *,
        created_at_ms: int,
        agent_md: str | None,
        replay_maxlen: int | None = None,
    ) -> None:
        self.created_at_ms = created_at_ms
        self.agent_md = agent_md
        self.current_request_id: str | None = None
        self.cancel_requested_for: str | None = None
        self._seq = 0
        maxlen = replay_maxlen if replay_maxlen is not None else _replay_maxlen_from_env()
        self._replay: deque[tuple[int, str]] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def publish_data(self, data_json: str) -> int:
        """Buffer and broadcast one JSON data event; returns monotonic sequence id."""
        async with self._lock:
            self._seq += 1
            seq = self._seq
            frame = format_data_event(seq, data_json)
            self._replay.append((seq, frame))
            subscribers = list(self._subscribers)
        for q in subscribers:
            await q.put(frame)
        return seq

    async def publish_comment(self, comment_line: str) -> None:
        """Send a comment/heartbeat line to live subscribers only (no replay)."""
        async with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            await q.put(comment_line)

    async def subscribe(
        self, last_event_id: int | None
    ) -> tuple[list[str], asyncio.Queue[str]]:
        """
        Register a subscriber and return buffered frames after last_event_id.

        If last_event_id is None, replay all buffered frames (seq > 0).
        """
        async with self._lock:
            q: asyncio.Queue[str] = asyncio.Queue()
            self._subscribers.add(q)
            cutoff = last_event_id if last_event_id is not None else 0
            replay_frames = [frame for seq, frame in self._replay if seq > cutoff]
        return replay_frames, q

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue (call from SSE disconnect finally)."""
        async with self._lock:
            self._subscribers.discard(queue)


class SessionRegistry:
    """Process-local registry of SessionBus instances."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionBus] = {}

    def get(self, session_id: str) -> SessionBus | None:
        """Return the bus for id or None."""
        return self._sessions.get(session_id)

    def create(self, session_id: str, *, agent_md: str | None, created_at_ms: int) -> SessionBus:
        """Create a new session bus or raise SessionAlreadyExistsError."""
        if session_id in self._sessions:
            raise SessionAlreadyExistsError(session_id)
        bus = SessionBus(created_at_ms=created_at_ms, agent_md=agent_md)
        self._sessions[session_id] = bus
        return bus
