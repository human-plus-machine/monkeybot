"""Session-scoped background subagent runs.

A background ``task`` returns immediately. The child keeps running on this
event loop after the turn that spawned it ends. Stop on a later turn does not
kill it: each run has its own cancel event. ``await_task`` watches the *turn*
cancel so Stop unblocks the wait and leaves the child alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import RunStore
from monkeybot.core.persistence.durable_runs import SubagentRunRow
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope
from monkeybot.core.subprocess_groups import kill_recorded_subagent

logger = logging.getLogger(__name__)

_PROCESS_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_WAKE_DEBOUNCE_S = 2.0
_MAX_CONSECUTIVE_WAKES = 3
_KILL_MESSAGE = "task: killed"
_ORPHAN_MESSAGE = "orphaned: gateway exited"


def process_owner_id() -> str:
    """Identity of this gateway process. Restart generates a new one."""
    return _PROCESS_OWNER


def classify_owner(owner_id: str | None, *, live_owner: str) -> str:
    """``live``, ``orphan`` (same host, dead process), or ``remote`` (other host)."""
    if not owner_id:
        return "orphan"
    if owner_id == live_owner:
        return "live"
    live_host = live_owner.split(":", 1)[0]
    owner_host = owner_id.split(":", 1)[0]
    if owner_host != live_host:
        return "remote"
    return "orphan"


def status_of_payload(payload: dict[str, Any]) -> str:
    reason = str(payload.get("exit_reason") or "")
    if reason == "completed" and payload.get("ok") is True:
        return "completed"
    if reason == "timeout":
        return "timeout"
    return "failed"


class BackgroundCapError(Exception):
    """The session is already at its background-child cap."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"session already has {limit} background subagents")
        self.limit = limit


@dataclass
class SubagentSpawn:
    """Everything the inline runner needs, plus session bookkeeping."""

    script: Path
    envelope: SubagentEnvelope
    scratch: Path
    child_env: dict[str, str]
    timeout: float
    ctx: Any
    call: ToolCall
    run_id: str
    child_thread_id: str
    subagent_type: str | None
    task: str
    session_id: str
    publisher: Any
    artifact_check: Callable[[], tuple[Any, list[Any]]] | None = None


@dataclass
class _Run:
    spawn: SubagentSpawn
    kill: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None
    awaited: bool = False
    woken: bool = False


WakeCallback = Callable[[list[tuple[str, str]]], Awaitable[bool]]


class SubagentSupervisor:
    """One per session. Outlives the turn that spawned a child."""

    def __init__(
        self,
        *,
        session_id: str,
        run_store: RunStore | None,
        max_background: int = 4,
        lease_ms: int = 60_000,
        owner_id: str | None = None,
        run_inline: Callable[..., Awaitable[dict[str, Any]]],
        on_wake: WakeCallback | None = None,
    ) -> None:
        self.session_id = session_id
        self._store = run_store
        self._max_background = max(1, max_background)
        self._lease_ms = max(1000, lease_ms)
        self._owner = owner_id or process_owner_id()
        self._run_inline = run_inline
        self._on_wake = on_wake
        self._runs: dict[str, _Run] = {}
        self._consecutive_wakes = 0
        self._wake_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def owner_id(self) -> str:
        return self._owner

    def running_count(self) -> int:
        return sum(1 for run in self._runs.values() if not run.finished.is_set())

    def note_human_turn(self) -> None:
        self._consecutive_wakes = 0

    def mark_awaited(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is not None:
            run.awaited = True

    def snapshot_lines(self) -> list[str]:
        """Lines for the volatile prompt note. Empty when nothing is outstanding."""
        lines: list[str] = []
        for run in self._runs.values():
            if run.finished.is_set():
                if run.awaited or run.payload is None:
                    continue
                status = status_of_payload(run.payload)
                lines.append(
                    f"- {run.spawn.run_id} finished ({status}). Call await_task before reporting."
                )
            else:
                lines.append(
                    f"- {run.spawn.run_id} running ({run.spawn.subagent_type or 'subagent'}). "
                    "Do not edit files it owns (expect_files)."
                )
        return lines

    def snapshot_text(self) -> str:
        lines = self.snapshot_lines()
        if not lines:
            return ""
        return (
            "\n".join(lines)
            + "\nRunning is not done. Call await_task before reporting results. Do not spin-poll."
        )

    def runs_still_need_wake(self, run_ids: tuple[str, ...]) -> bool:
        for run_id in run_ids:
            run = self._runs.get(run_id)
            if run is None or not run.awaited:
                return True
        return False

    def pending_wake(self) -> list[tuple[str, str]]:
        """Terminal runs not yet awaited and not yet woken."""
        out: list[tuple[str, str]] = []
        for run in self._runs.values():
            if not run.finished.is_set() or run.awaited or run.woken or run.payload is None:
                continue
            out.append((run.spawn.run_id, status_of_payload(run.payload)))
        return out

    async def spawn(self, spec: SubagentSpawn) -> None:
        if self._closed:
            raise BackgroundCapError(self._max_background)
        if self.running_count() >= self._max_background:
            raise BackgroundCapError(self._max_background)
        run = _Run(spawn=spec)
        self._runs[spec.run_id] = run
        if self._store is not None:
            await self._store.record_started(
                spec.run_id,
                spec.envelope.parent_run_id,
                str(spec.script),
                spec.envelope,
                spec.scratch,
                owner_id=self._owner,
            )
        run.task = asyncio.create_task(self._drive(run), name=f"subagent-{spec.run_id}")

    async def await_run(
        self,
        run_id: str,
        *,
        session_id: str,
        wait_seconds: float,
        turn_cancelled: asyncio.Event | None,
    ) -> dict[str, Any] | None:
        """Return the run payload, a running snapshot, or None when unknown.

        ``turn_cancelled`` ends the wait without killing the child.
        """
        run = self._runs.get(run_id)
        if run is None:
            return await self._from_store(run_id, session_id)
        if run.spawn.session_id != session_id:
            return None
        if run.finished.is_set() and run.payload is not None:
            run.awaited = True
            return _terminal(run.payload)
        waiters: set[asyncio.Task[Any]] = {asyncio.create_task(run.finished.wait())}
        cancel_wait = None
        if turn_cancelled is not None:
            cancel_wait = asyncio.create_task(turn_cancelled.wait())
            waiters.add(cancel_wait)
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=max(0.0, wait_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter
        if run.finished.is_set() and run.payload is not None:
            run.awaited = True
            return _terminal(run.payload)
        del done
        return _running_snapshot(run)

    async def kill(self, run_id: str, *, session_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if run is None:
            stored = await self._from_store(run_id, session_id)
            if stored is None:
                return None
            if stored.get("status") == "running":
                return {
                    "ok": False,
                    "status": "running",
                    "error_kind": "owned_by_other_instance",
                    "run_id": run_id,
                    "message": "This run is owned by another gateway process.",
                }
            return stored
        if run.spawn.session_id != session_id:
            return None
        if not run.finished.is_set():
            run.kill.set()
            if run.task is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(run.task), timeout=10)
        if run.payload is None:
            return _running_snapshot(run)
        run.awaited = True
        return _terminal(run.payload)

    async def aclose(self, reason: str) -> None:
        self._closed = True
        if self._wake_task is not None and not self._wake_task.done():
            self._wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_task
        for run in list(self._runs.values()):
            if not run.finished.is_set():
                run.kill.set()
        tasks = [
            run.task for run in self._runs.values() if run.task is not None and not run.task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run in self._runs.values():
            if run.payload is not None:
                continue
            await self._record_terminal(
                run,
                {
                    "ok": False,
                    "exit_reason": "killed",
                    "errors": [reason],
                    "final_message": "",
                    "run_id": run.spawn.run_id,
                    "child_thread_id": run.spawn.child_thread_id,
                    "subagent_type": run.spawn.subagent_type,
                },
            )

    async def _drive(self, run: _Run) -> dict[str, Any]:
        spec = run.spawn
        heartbeat: asyncio.Task[None] | None = None
        if self._store is not None and self._lease_ms > 0:
            heartbeat = asyncio.create_task(
                self._heartbeat(spec.run_id), name=f"lease-{spec.run_id}"
            )
        try:
            payload = await self._run_inline(
                script=spec.script,
                envelope=spec.envelope,
                scratch=spec.scratch,
                child_env=spec.child_env,
                timeout=spec.timeout,
                ctx=spec.ctx,
                call=spec.call,
                run_id=spec.run_id,
                child_thread_id=spec.child_thread_id,
                subagent_type=spec.subagent_type,
                task=spec.task,
                publisher=spec.publisher,
                request_id="",
                cancelled=run.kill,
                cancel_message=_KILL_MESSAGE,
                cancel_exit_reason="killed",
            )
            if spec.artifact_check is not None and not run.kill.is_set():
                try:
                    exists, artifacts = spec.artifact_check()
                    payload["artifact_exists"] = exists
                    payload["artifacts"] = artifacts
                except Exception:
                    logger.warning(
                        "background artifact check failed %s", kv(run_id=spec.run_id), exc_info=True
                    )
            run.payload = payload
            await self._record_terminal(run, payload)
            return payload
        except Exception as exc:
            logger.warning(
                "background subagent failed %s",
                kv(run_id=spec.run_id, error=str(exc)),
                exc_info=True,
            )
            payload = {
                "ok": False,
                "exit_reason": "error",
                "errors": [str(exc)],
                "final_message": "",
                "run_id": spec.run_id,
                "child_thread_id": spec.child_thread_id,
                "subagent_type": spec.subagent_type,
            }
            run.payload = payload
            await self._record_terminal(run, payload)
            return payload
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat
            run.finished.set()
            if not self._closed and not run.awaited:
                self._schedule_wake()

    async def _heartbeat(self, run_id: str) -> None:
        interval = max(1.0, (self._lease_ms / 1000.0) / 4.0)
        store = self._store
        if store is None:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await store.renew_claim(run_id, self._owner)
            except Exception:
                logger.warning("subagent lease renew failed %s", kv(run_id=run_id), exc_info=True)
                return
            if not ok:
                return

    async def _record_terminal(self, run: _Run, payload: dict[str, Any]) -> None:
        store = self._store
        if store is None:
            return
        import json

        encoded = json.dumps(payload, ensure_ascii=False)
        errors = list(payload.get("errors") or [])
        try:
            if payload.get("ok") is True and not errors:
                await store.record_completed(run.spawn.run_id, encoded, worker_id=self._owner)
            else:
                message = "; ".join(str(e) for e in errors) or str(
                    payload.get("exit_reason") or "error"
                )
                await store.record_failed(
                    run.spawn.run_id,
                    message,
                    worker_id=self._owner,
                    result_json=encoded,
                )
        except Exception:
            logger.warning(
                "subagent outcome persist failed %s", kv(run_id=run.spawn.run_id), exc_info=True
            )

    async def _from_store(self, run_id: str, session_id: str) -> dict[str, Any] | None:
        store = self._store
        if store is None:
            return None
        row = await store.get_run(run_id)
        if row is None:
            return None
        if not _row_session(row, session_id):
            return None
        if row.status == "running":
            kind = classify_owner(row.worker_id, live_owner=self._owner)
            if kind == "orphan":
                await store.record_failed(run_id, _ORPHAN_MESSAGE, result_json=row.result_json)
                kill_recorded_subagent(Path(row.scratch_dir))
                return {
                    "ok": False,
                    "status": "failed",
                    "exit_reason": "error",
                    "errors": [_ORPHAN_MESSAGE],
                    "run_id": run_id,
                    "final_message": "",
                }
            if kind == "remote":
                return {
                    "ok": True,
                    "status": "running",
                    "run_id": run_id,
                    "error_kind": "owned_by_other_instance",
                    "message": "This run is owned by another gateway process.",
                }
            return {"ok": True, "status": "running", "run_id": run_id}
        return _stored_terminal(row)

    def _schedule_wake(self) -> None:
        if self._on_wake is None or self._closed:
            return
        if self._wake_task is not None and not self._wake_task.done():
            return

        async def _flush() -> None:
            try:
                await asyncio.sleep(_WAKE_DEBOUNCE_S)
                await self._emit_wake()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "subagent wake failed %s", kv(session_id=self.session_id), exc_info=True
                )

        self._wake_task = asyncio.create_task(_flush(), name=f"subagent-wake-{self.session_id}")

    async def wake_if_idle(self) -> None:
        """Send a deferred wake once the session is idle.

        A completion that landed during a user turn leaves the batch pending.
        The turn end calls this after clearing ``current_request_id``.
        """
        task = self._wake_task
        if task is not None and not task.done():
            return
        try:
            await self._emit_wake()
        except Exception:
            logger.warning("subagent wake failed %s", kv(session_id=self.session_id), exc_info=True)

    async def _emit_wake(self) -> None:
        batch = self.pending_wake()
        if not batch or self._on_wake is None or self._consecutive_wakes >= _MAX_CONSECUTIVE_WAKES:
            return
        enqueued = await self._on_wake(batch)
        if not enqueued:
            return
        for run_id, _status in batch:
            run = self._runs.get(run_id)
            if run is not None:
                run.woken = True
        self._consecutive_wakes += 1


def _running_snapshot(run: _Run) -> dict[str, Any]:
    spec = run.spawn
    return {
        "ok": True,
        "status": "running",
        "run_id": spec.run_id,
        "child_thread_id": spec.child_thread_id,
        "subagent_type": spec.subagent_type,
    }


def _terminal(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out["status"] = status_of_payload(payload)
    return out


def _row_session(row: SubagentRunRow, session_id: str) -> bool:
    try:
        envelope = SubagentEnvelope.from_json(row.envelope_json)
    except Exception:
        return False
    return envelope.parent_session_id == session_id


def _stored_terminal(row: SubagentRunRow) -> dict[str, Any]:
    import json

    if row.result_json:
        try:
            payload = json.loads(row.result_json)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return _terminal(payload)
    message = row.error_json or "failed"
    return {
        "ok": False,
        "status": "failed"
        if row.status == "failed"
        else status_of_payload({"exit_reason": row.status, "ok": row.status == "completed"}),
        "exit_reason": "error" if row.status == "failed" else "completed",
        "errors": [message],
        "final_message": "",
        "run_id": row.run_id,
    }


async def fail_orphaned_runs(store: RunStore, *, live_owner: str | None = None) -> int:
    """Mark this host's ``running`` rows from a dead process as failed."""
    owner = live_owner or process_owner_id()
    failed = 0
    for row in await store.list_running():
        if classify_owner(row.worker_id, live_owner=owner) != "orphan":
            continue
        scratch = Path(row.scratch_dir)
        kill_recorded_subagent(scratch)
        ok = await store.record_failed(row.run_id, _ORPHAN_MESSAGE, result_json=row.result_json)
        if ok:
            failed += 1
            logger.warning(
                "orphaned subagent run %s", kv(run_id=row.run_id, owner=row.worker_id or "")
            )
    return failed
