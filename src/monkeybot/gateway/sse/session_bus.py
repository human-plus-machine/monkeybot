"""
In-memory per-session SSE bus with replay buffer and live subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.config.snapshot import current_env
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.transcript import TranscriptWriter
from monkeybot.core.runtime.input_admission import InputAdmission
from monkeybot.core.tools.permission import SessionApprovals
from monkeybot.gateway.pending_response_bus import TERMINATED_PENDING_KEYS_MAXLEN
from monkeybot.todo_list.store import TodoListStore

from .sse import format_data_event

logger = logging.getLogger(__name__)

# Soft-cancel wait before hard-cancelling an in-flight turn on session delete.
_QUIESCE_TURN_TIMEOUT_SEC: float = float(os.environ.get("MONKEYBOT_QUIESCE_TURN_TIMEOUT_SEC", "30"))
_QUIESCE_HARD_CANCEL_TIMEOUT_SEC: float = 5.0

ReplayLane = Literal["primary", "nested"]


@dataclass(frozen=True)
class RemoveResult:
    """Outcome of removing a session from the registry."""

    deleted: bool
    transcript_dir: str | None = None


def _replay_maxlen_from_env() -> int:
    raw = current_env("SSE_REPLAY_MAX", "256")
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        return 256


def _nested_replay_maxlen_from_env() -> int:
    """Nested subagent traffic uses a separate replay lane (SSE_NESTED_REPLAY_MAX)."""
    raw = current_env("SSE_NESTED_REPLAY_MAX", "").strip()
    if not raw:
        return _replay_maxlen_from_env()
    try:
        return max(1, int(raw))
    except ValueError:
        return _replay_maxlen_from_env()


class SessionAlreadyExistsError(Exception):
    """Raised when POST /sessions repeats an existing client-supplied id."""


class SessionBus:
    """Broadcasts framed SSE events; buffers numbered data events for replay.

    Primary-turn and nested-subagent events share one monotonic ``_seq`` (for
    Last-Event-ID ordering) but use **partitioned** replay deques so a chatty
    subagent cannot evict parent-turn frames from the primary lane.
    """

    def __init__(
        self,
        *,
        created_at_ms: int,
        agent_md: str | None,
        replay_maxlen: int | None = None,
        nested_replay_maxlen: int | None = None,
        provider: Any | None = None,
        model_name: str | None = None,
    ) -> None:
        self.created_at_ms = created_at_ms
        self.agent_md = agent_md
        self.provider = provider
        self.model_name = model_name
        self.current_request_id: str | None = None
        self.cancel_requested_for: str | None = None
        self.turn_cancel_event: asyncio.Event | None = None
        self._seq = 0
        primary_maxlen = replay_maxlen if replay_maxlen is not None else _replay_maxlen_from_env()
        nested_maxlen = (
            nested_replay_maxlen
            if nested_replay_maxlen is not None
            else _nested_replay_maxlen_from_env()
        )
        self._replay_primary: deque[tuple[int, str]] = deque(maxlen=primary_maxlen)
        self._replay_nested: deque[tuple[int, str]] = deque(maxlen=nested_maxlen)
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()
        self.pending_responses: dict[str, asyncio.Future[Any]] = {}
        self.terminated_pending_keys: deque[str] = deque(maxlen=TERMINATED_PENDING_KEYS_MAXLEN)
        self.attachment_catalog: SessionAttachmentCatalog | None = None
        self.todo_store: TodoListStore | None = None
        """Process-local session todo list (not shared across gateway replicas)."""
        self.transcript_writer: TranscriptWriter | None = None
        """Lazily-created ``TranscriptWriter`` (internal debugging only); None when disabled."""
        self.admission = InputAdmission()
        """Process-local steer + follow-up queues (not shared across gateway replicas)."""
        self.session_approvals = SessionApprovals()
        """Process-local 'always allow' rememberies (not shared across gateway replicas)."""
        self.follow_up_retry_task: asyncio.Task[None] | None = None
        """Scheduled drain retry after a failed durable turn-lock acquire."""
        self.active_turn_task: asyncio.Task[None] | None = None
        """Background turn task scheduled by ``_schedule_turn``; awaited on DELETE."""

    def request_cancel(self, request_id: str) -> None:
        """Record user Stop and set the in-flight turn event before futures are cancelled.

        POST /cancel used to only set ``cancel_requested_for``; a 50ms poller then
        flipped the turn Event. Confirm ``CancelledError`` always won that race, so
        Stop-during-HITL never settled. Set the bound Event here, synchronously.
        """
        self.cancel_requested_for = request_id
        event = self.turn_cancel_event
        if event is not None and request_id == self.current_request_id:
            event.set()

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

    def is_pending_or_terminal(
        self, pending_key: str
    ) -> Literal["pending", "terminated", "unknown"]:
        if pending_key in self.pending_responses:
            return "pending"
        if pending_key in self.terminated_pending_keys:
            return "terminated"
        return "unknown"

    def _replay_lane(self, lane: ReplayLane) -> deque[tuple[int, str]]:
        return self._replay_nested if lane == "nested" else self._replay_primary

    def _merged_replay(self) -> list[tuple[int, str]]:
        """Primary + nested frames sorted by shared sequence id."""
        return sorted(
            (*self._replay_primary, *self._replay_nested),
            key=lambda item: item[0],
        )

    async def publish_data(self, data_json: str, *, lane: ReplayLane = "primary") -> int:
        """Buffer and broadcast one JSON data event; returns monotonic sequence id.

        ``lane`` selects which partitioned replay deque stores the frame. Nested
        subagent progress should use ``lane="nested"`` so it cannot evict
        primary-turn events.
        """
        async with self._lock:
            self._seq += 1
            seq = self._seq
            frame = format_data_event(seq, data_json)
            self._replay_lane(lane).append((seq, frame))
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

    async def subscribe(self, last_event_id: int | None) -> tuple[list[str], asyncio.Queue[str]]:
        """
        Register a subscriber and return buffered frames after last_event_id.

        If last_event_id is None, replay all buffered frames (seq > 0).
        Merges primary and nested lanes in sequence order.
        """
        async with self._lock:
            q: asyncio.Queue[str] = asyncio.Queue()
            self._subscribers.add(q)
            cutoff = last_event_id if last_event_id is not None else 0
            replay_frames = [frame for seq, frame in self._merged_replay() if seq > cutoff]
        return replay_frames, q

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue (call from SSE disconnect finally)."""
        async with self._lock:
            self._subscribers.discard(queue)

    async def quiesce_active_turn(
        self,
        *,
        timeout_sec: float | None = None,
    ) -> None:
        """Soft-cancel the in-flight turn, await it, then drain the transcript writer.

        Teardown must not race late ``ToolCallResult`` / ``TurnComplete`` appends.
        """
        rid = self.current_request_id
        if rid is not None:
            self.cancel_requested_for = rid
        task = self.active_turn_task
        wait_sec = _QUIESCE_TURN_TIMEOUT_SEC if timeout_sec is None else timeout_sec
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_sec)
            except TimeoutError:
                logger.warning("active turn did not finish before quiesce timeout; hard-cancelling")
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, timeout=_QUIESCE_HARD_CANCEL_TIMEOUT_SEC)
            except asyncio.CancelledError:
                pass
        writer = self.transcript_writer
        if writer is not None:
            await writer.drain()


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
    from monkeybot.core.runtime.tool_batch import _await_user_response_any

    fut = bus.pending_responses[pending_key]
    return await _await_user_response_any(bus, fut, pending_key, timeout_sec=timeout_sec)


class SessionRegistry:
    """Process-local registry of SessionBus instances."""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self._sessions: dict[
            str, SessionBus
        ] = {}  # ponytail: in-process registry, use Redis pub/sub for multi-instance deployments
        self._workspace_root = workspace_root

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

    def iter_buses(self) -> list[SessionBus]:
        """Snapshot of live session buses (does not mutate the registry)."""
        return list(self._sessions.values())

    def _workspace_for_spill(self) -> Path:
        """Workspace root for spill cleanup: injected path or ``paths.workspace_root`` from yaml."""
        if self._workspace_root is not None:
            return Path(self._workspace_root).resolve()
        from monkeybot.core.workspace_layout import resolve_agent_workspace_root

        return resolve_agent_workspace_root()

    def _detach(self, session_id: str) -> SessionBus | None:
        """Pop a session and clear in-process auxiliaries; does not drain spill."""
        bus = self._sessions.pop(session_id, None)
        if bus is None:
            return None
        bus.abandon_pending_cancel_all()
        bus.cancel_follow_up_retry()
        bus.admission.clear_all()
        return bus

    async def _cleanup_spill(self, session_id: str) -> None:
        """Best-effort concurrent removal of session + subagent spill dirs."""
        from monkeybot.core.tools.spill_inventory import cleanup_session_spill_files

        try:
            await cleanup_session_spill_files(self._workspace_for_spill(), session_id)
        except Exception:
            logger.warning(
                "spill cleanup failed %s",
                kv(session_id=session_id),
                exc_info=True,
            )

    def remove(self, session_id: str) -> RemoveResult:
        """Drop a session bus and any auxiliary per-session state keyed by it.

        Cancels outstanding pending-response futures so awaiting callers don't
        hang.

        Does not run spill cleanup — use :meth:`remove_async` for that
        (DELETE /sessions and gateway shutdown).
        """
        bus = self._detach(session_id)
        if bus is None:
            return RemoveResult(deleted=False)
        return RemoveResult(deleted=True)

    async def remove_async(self, session_id: str) -> RemoveResult:
        """Detach session, quiesce the turn, drain the transcript writer, clean spill."""
        bus = self._detach(session_id)
        if bus is None:
            return RemoveResult(deleted=False)
        try:
            # Quiesce before spill cleanup so an in-flight turn cannot rewrite or
            # read spill dirs after we delete them. quiesce_active_turn drains
            # the transcript writer.
            await bus.quiesce_active_turn()
        finally:
            await self._cleanup_spill(session_id)
        writer = bus.transcript_writer
        if writer is None:
            return RemoveResult(deleted=True)
        return RemoveResult(deleted=True, transcript_dir=str(writer.session_dir))

    async def remove_all_async(self) -> None:
        """Best-effort remove every remaining session (gateway shutdown).

        Session removals (including spill cleanup) run concurrently via
        ``asyncio.gather`` so shutdown time doesn't scale linearly with the
        number of open sessions.
        """
        results = await asyncio.gather(
            *(self.remove_async(sid) for sid in list(self._sessions)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("session remove on shutdown failed", exc_info=result)
