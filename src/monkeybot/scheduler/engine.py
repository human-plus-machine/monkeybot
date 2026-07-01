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
from monkeybot.scheduler.tick_result import TickInvokeResult, TickInvokeStatus

logger = logging.getLogger(__name__)

_DEFAULT_HEARTBEAT_INTERVAL_S = 30.0


class SessionBusyChecker(Protocol):
    def is_busy(self, session_id: str) -> bool: ...


class AsyncSessionBusyChecker(Protocol):
    async def is_busy_async(self, session_id: str) -> bool: ...


class SessionEnsurer(Protocol):
    async def ensure_session(self, session_id: str) -> None: ...


class TickInvoker(Protocol):
    async def invoke_tick(
        self,
        session_id: str,
        request_id: str,
        user_content: list[ContentBlock],
    ) -> TickInvokeResult:
        """Run one scheduled tick."""


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
    max_concurrency: int
    claim_heartbeat_interval_s: float


def scheduler_settings() -> SchedulerSettings:
    stale_claim_ms = _env_int("MONKEYBOT_SCHEDULER_STALE_CLAIM_MS", 600_000)
    heartbeat_raw = os.environ.get("MONKEYBOT_SCHEDULER_CLAIM_HEARTBEAT_S", "").strip()
    if heartbeat_raw:
        heartbeat_s = _env_float("MONKEYBOT_SCHEDULER_CLAIM_HEARTBEAT_S", _DEFAULT_HEARTBEAT_INTERVAL_S)
    else:
        heartbeat_s = min(_DEFAULT_HEARTBEAT_INTERVAL_S, max(5.0, stale_claim_ms / 4000.0))
    return SchedulerSettings(
        poll_interval_s=_env_float("MONKEYBOT_SCHEDULER_POLL_INTERVAL_S", 5.0),
        stale_claim_ms=stale_claim_ms,
        max_concurrency=max(1, _env_int("MONKEYBOT_SCHEDULER_CONCURRENCY", 1)),
        claim_heartbeat_interval_s=heartbeat_s,
    )


def scheduler_enabled_from_env() -> bool:
    raw = os.environ.get("MONKEYBOT_SCHEDULER_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_scheduler_worker_id() -> str:
    explicit = os.environ.get("MONKEYBOT_SCHEDULER_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return f"scheduler-{uuid.uuid4().hex[:12]}"


async def _claim_heartbeat(
    *,
    store: ScheduledLoopStore,
    loop_id: str,
    worker_id: str,
    interval_s: float,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return
        except TimeoutError:
            renewed = await store.renew_tick_claim(loop_id, worker_id)
            if not renewed:
                return


async def run_scheduler_loop(
    *,
    store: ScheduledLoopStore,
    invoker: TickInvoker,
    session_busy: SessionBusyChecker,
    ensure_session: SessionEnsurer,
    worker_id: str,
    poll_interval_s: float = 5.0,
    stale_claim_ms: int = 600_000,
    max_concurrency: int = 1,
    claim_heartbeat_interval_s: float = _DEFAULT_HEARTBEAT_INTERVAL_S,
    shutdown: asyncio.Event | None = None,
) -> None:
    """Poll due loops and invoke agent ticks until ``shutdown`` is set."""
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    tick_tasks: set[asyncio.Task[None]] = set()

    def _prune_done() -> None:
        tick_tasks.difference_update({task for task in tick_tasks if task.done()})

    while shutdown is None or not shutdown.is_set():
        try:
            released = await store.release_stale_claims(stale_claim_ms)
            if released:
                logger.warning("scheduler released %d stale tick claims", released)
            now_ms = int(time.time() * 1000)
            due = await store.list_due(now_ms)
            claimed_rows: list[ScheduledLoopRow] = []
            for candidate in due:
                if shutdown is not None and shutdown.is_set():
                    break
                claimed = await store.claim_tick(candidate.loop_id, worker_id)
                if claimed is not None:
                    claimed_rows.append(claimed)

            async def _run_claimed(row: ScheduledLoopRow) -> None:
                async with semaphore:
                    await _execute_claimed_tick(
                        store=store,
                        invoker=invoker,
                        session_busy=session_busy,
                        ensure_session=ensure_session,
                        row=row,
                        worker_id=worker_id,
                        claim_heartbeat_interval_s=claim_heartbeat_interval_s,
                    )

            for row in claimed_rows:
                if shutdown is not None and shutdown.is_set():
                    break
                task = asyncio.create_task(
                    _run_claimed(row),
                    name=f"scheduler-tick-{row.loop_id}",
                )
                tick_tasks.add(task)
                task.add_done_callback(tick_tasks.discard)
            _prune_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler poll loop error worker_id=%s", worker_id)
        if shutdown is not None and shutdown.is_set():
            break
        await asyncio.sleep(poll_interval_s)

    if tick_tasks:
        await asyncio.gather(*tick_tasks, return_exceptions=True)


async def _session_is_busy(session_busy: SessionBusyChecker, session_id: str) -> bool:
    async_check = getattr(session_busy, "is_busy_async", None)
    if async_check is not None:
        return bool(await async_check(session_id))
    return session_busy.is_busy(session_id)


async def _execute_claimed_tick(
    *,
    store: ScheduledLoopStore,
    invoker: TickInvoker,
    session_busy: SessionBusyChecker,
    ensure_session: SessionEnsurer,
    row: ScheduledLoopRow,
    worker_id: str,
    claim_heartbeat_interval_s: float,
) -> None:
    if row.skip_if_busy and await _session_is_busy(session_busy, row.session_id):
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

    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _claim_heartbeat(
            store=store,
            loop_id=row.loop_id,
            worker_id=worker_id,
            interval_s=claim_heartbeat_interval_s,
            stop=heartbeat_stop,
        ),
        name=f"scheduler-heartbeat-{row.loop_id}",
    )
    try:
        await ensure_session.ensure_session(row.session_id)
        request_id = f"loop-{row.loop_id}-{row.tick_index + 1}-{uuid.uuid4().hex[:8]}"
        tick_prompt = format_tick_prompt(row)
        result = await invoker.invoke_tick(
            row.session_id,
            request_id,
            [Text(text=tick_prompt)],
        )
        if result.status is TickInvokeStatus.SESSION_BUSY:
            if row.skip_if_busy:
                await store.defer_tick(
                    row.loop_id,
                    worker_id=worker_id,
                    reason="session busy; deferred",
                )
                logger.info(
                    "scheduler deferred tick loop_id=%s session_id=%s (remote busy)",
                    row.loop_id,
                    row.session_id,
                )
                return
            await store.complete_tick(
                row.loop_id,
                worker_id=worker_id,
                error="session busy",
            )
            return

        error = result.error if result.status is TickInvokeStatus.ERROR else None
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
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


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
    max_concurrency: int | None = None,
    claim_heartbeat_interval_s: float | None = None,
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
            max_concurrency=(
                max_concurrency if max_concurrency is not None else settings.max_concurrency
            ),
            claim_heartbeat_interval_s=(
                claim_heartbeat_interval_s
                if claim_heartbeat_interval_s is not None
                else settings.claim_heartbeat_interval_s
            ),
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
