"""
In-memory per-session SSE bus with replay buffer and live subscribers.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from typing import Any, Literal

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.input_admission import InputAdmission

from .sse import format_data_event

PENDING_RESPONSE_TIMEOUT_SEC: float = float(os.environ.get("PENDING_RESPONSE_TIMEOUT_SEC", "300"))


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
        provider: Any | None = None,
        model_name: str | None = None,
    ) -> None:
        self.created_at_ms = created_at_ms
        self.agent_md = agent_md
        self.provider = provider
        self.model_name = model_name
        self.current_request_id: str | None = None
        self.cancel_requested_for: str | None = None
        self._seq = 0
        maxlen = replay_maxlen if replay_maxlen is not None else _replay_maxlen_from_env()
        self._replay: deque[tuple[int, str]] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()
        self.pending_responses: dict[str, asyncio.Future[Any]] = {}
        self.terminated_pending_keys: deque[str] = deque(maxlen=256)
        self.attachment_catalog: SessionAttachmentCatalog | None = None
        self.transcript_writer: TranscriptWriter | None = None
        """Lazily-created ``TranscriptWriter`` (internal debugging only); None when disabled."""
        self.admission = InputAdmission()
        """Process-local steer + follow-up queues (not shared across gateway replicas)."""
        self.follow_up_retry_task: asyncio.Task[None] | None = None
        """Scheduled drain retry after a failed durable turn-lock acquire."""

    def cancel_follow_up_retry(self) -> None:
        """Cancel any pending follow-up lock-retry task."""
        task = self.follow_up_retry_task
        self.follow_up_retry_task = None
        if task is not None and not task.done():
            task.cancel()

    def register_pending(self, pending_key: str) -> asyncio.Future[Any]:
        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[pending_key] = fut
        return fut

    def resolve_pending(self, pending_key: str, payload: Any) -> bool:
        fut = self.pending_responses.get(pending_key)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        self.pending_responses.pop(pending_key, None)
        self.terminated_pending_keys.append(pending_key)
        return True

    def abandon_pending_timeout(self, pending_key: str) -> None:
        fut = self.pending_responses.pop(pending_key, None)
        if fut is None:
            return
        if not fut.done():
            fut.cancel()
        self.terminated_pending_keys.append(pending_key)

    def abandon_pending_cancel_all(self) -> None:
        for pending_key in list(self.pending_responses.keys()):
            fut = self.pending_responses.pop(pending_key, None)
            if fut is not None and not fut.done():
                fut.cancel()
            self.terminated_pending_keys.append(pending_key)

    def is_pending_or_terminal(self, pending_key: str) -> Literal["pending", "terminated", "unknown"]:
        if pending_key in self.pending_responses:
            return "pending"
        if pending_key in self.terminated_pending_keys:
            return "terminated"
        return "unknown"

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


async def _await_user_response(
    bus: SessionBus,
    *,
    pending_key: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Block until POST resolves *pending_key*, timeout, cancel, or disconnect policy.

    Returns:
        Normal POST payloads (structure depends on flow).
        On timeout: ``{"_timeout": True}`` (sentinel).

    Raises:
        asyncio.CancelledError: When the backing Future was cancelled (Stop button path).
    """
    from monkeybot.core.runtime.loop import _await_user_response_any

    fut = bus.pending_responses[pending_key]
    return await _await_user_response_any(bus, fut, pending_key, timeout_sec=timeout_sec)


class SessionRegistry:
    """Process-local registry of SessionBus instances."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionBus] = {}  # ponytail: in-process registry, use Redis pub/sub for multi-instance deployments

    def get(self, session_id: str) -> SessionBus | None:
        """Return the bus for id or None."""
        return self._sessions.get(session_id)

    def create(
        self,
        session_id: str,
        *,
        agent_md: str | None,
        created_at_ms: int,
        provider: Any | None = None,
        model_name: str | None = None,
    ) -> SessionBus:
        """Create a new session bus or raise SessionAlreadyExistsError."""
        if session_id in self._sessions:
            raise SessionAlreadyExistsError(session_id)
        bus = SessionBus(
            created_at_ms=created_at_ms,
            agent_md=agent_md,
            provider=provider,
            model_name=model_name,
        )
        bus.attachment_catalog = SessionAttachmentCatalog(session_id=session_id)
        self._sessions[session_id] = bus
        return bus

    def remove(self, session_id: str) -> bool:
        """Drop a session bus and any auxiliary per-session state keyed by it.

        Cancels outstanding pending-response futures so awaiting callers don't
        hang, then evicts the memory-curation cache entry for this thread id
        (see ``memory_prompt._curation_cache``) so both structures share the
        same lifecycle instead of growing unbounded for the life of the process.

        Returns True if a session was found and removed, False otherwise.
        """
        bus = self._sessions.pop(session_id, None)
        if bus is None:
            return False
        bus.abandon_pending_cancel_all()
        bus.cancel_follow_up_retry()
        bus.admission.clear_all()

        from monkeybot.core.context.memory_prompt import evict_curation_cache

        evict_curation_cache(session_id)
        return True
