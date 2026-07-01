"""Poll-and-fire engine for prompt-first scheduled agent loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.core.persistence.scheduled_loops import ScheduledLoopRow, format_tick_prompt
from monkeybot.core.types.content_blocks import ContentBlock, Text

logger = logging.getLogger(__name__)


class SessionBusyChecker(Protocol):
    def is_busy(self, session_id: str) -> bool: ...


class SessionEnsurer(Protocol):
    async def ensure_session(self, session_id: str) -> None: ...


class TickInvoker(Protocol):
    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> str | None:
        """Run one scheduled tick; return error text or None on success."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class SchedulerSettings:
    poll_interval_s: float
    stale_claim_ms: int


def scheduler_settings() -> SchedulerSettings:
    return SchedulerSettings(
        poll_interval_s=_env_float("MONKEYBOT_SCHEDULER_POLL_INTERVAL_S", 5.0),
        stale_claim_ms=_env_int("MONKEYBOT_SCHEDULER_STALE_CLAIM_MS", 600_000),
    )


def scheduler_enabled_from_env() -> bool:
    raw = os.environ.get("MONKEYBOT_SCHEDULER_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_scheduler_worker_id() -> str:
    explicit = os.environ.get("MONKEYBOT_SCHEDULER_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return f"scheduler-{uuid.uuid4().hex[:12]}"


async def run_scheduler_loop(
    *,
    store: ScheduledLoopStore,
    invoker: TickInvoker,
  session_busy: SessionBusyChecker,
  ensure_session: SessionEnsurer,
    worker_id: str,
    poll_interval_s: float = 5.0,
    stale_claim_ms: int = 600_000,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Poll due loops and invoke agent ticks until ``shutdown`` is set."""
    while shutdown is None or not shutdown.is_set():
        try:
            released = await store.release_stale_claims(stale_claim_ms)
            if released:
                logger.warning("scheduler released %d stale tick claims", released)
            now_ms = int(time.time() * 1000)
            due = await store.list_due(now_ms)
            for candidate in due:
                if shutdown is not None and shutdown.is_set():
                    break
                claimed = await store.claim_tick(candidate.loop_id, worker_id)
                if claimed is None:
                    continue
                await _execute_claimed_tick(
                    store=store,
                    invoker=invoker,
                    session_busy=session_busy,
                    ensure_session=ensure_session,
                    row=claimed,
                    worker_id=worker_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler poll loop error worker_id=%s", worker_id)
        if shutdown is not None and shutdown.is_set():
            break
        await asyncio.sleep(poll_interval_s)


async def _execute_claimed_tick(
    *,
    store: ScheduledLoopStore,
    invoker: TickInvoker,
    session_busy: SessionBusyChecker,
    ensure_session: SessionEnsurer,
    row: ScheduledLoopRow,
    worker_id: str,
) -> None:
    if row.skip_if_busy and session_busy.is_busy(row.session_id):
        await store.defer_tick(
            row.loop_id,
            worker_id=worker_id,
            reason="session busy; deferred",
        )
        logger.info(
            "scheduler deferred tick loop_id=%s session_id=%s (busy)",
            row.loop_id,
            row.session_id,
        )
        return
    try:
        await ensure_session.ensure_session(row.session_id)
        request_id = f"loop-{row.loop_id}-{row.tick_index + 1}-{uuid.uuid4().hex[:8]}"
        tick_prompt = format_tick_prompt(row)
        error = await invoker.invoke_tick(
            row.session_id,
            request_id,
            [Text(text=tick_prompt)],
        )
        await store.complete_tick(row.loop_id, worker_id=worker_id, error=error)
        if error:
            logger.warning(
                "scheduler tick failed loop_id=%s session_id=%s error=%s",
                row.loop_id,
                row.session_id,
                error,
            )
        else:
            logger.info(
                "scheduler tick ok loop_id=%s session_id=%s tick=%d",
                row.loop_id,
                row.session_id,
                row.tick_index + 1,
            )
    except Exception as exc:
        logger.exception("scheduler tick exception loop_id=%s", row.loop_id)
        await store.complete_tick(row.loop_id, worker_id=worker_id, error=str(exc))


@dataclass
class SchedulerHandle:
    task: asyncio.Task[None]
    shutdown: asyncio.Event


def start_scheduler_background(
    *,
    store: ScheduledLoopStore,
    invoker: TickInvoker,
    session_busy: SessionBusyChecker,
    ensure_session: SessionEnsurer,
    worker_id: str | None = None,
    poll_interval_s: float | None = None,
    stale_claim_ms: int | None = None,
) -> SchedulerHandle:
    settings = scheduler_settings()
    shutdown = asyncio.Event()
    wid = worker_id or resolve_scheduler_worker_id()
    task = asyncio.create_task(
        run_scheduler_loop(
            store=store,
            invoker=invoker,
            session_busy=session_busy,
            ensure_session=ensure_session,
            worker_id=wid,
            poll_interval_s=(
                poll_interval_s if poll_interval_s is not None else settings.poll_interval_s
            ),
            stale_claim_ms=stale_claim_ms if stale_claim_ms is not None else settings.stale_claim_ms,
            shutdown=shutdown,
        ),
        name=f"monkeybot-scheduler-{wid}",
    )
    return SchedulerHandle(task=task, shutdown=shutdown)


async def shutdown_scheduler(handle: SchedulerHandle) -> None:
    handle.shutdown.set()
    handle.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await handle.task
