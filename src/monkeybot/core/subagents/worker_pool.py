"""Poll-and-claim worker pool for queued subagent runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import RunStore, StorageBackend
from monkeybot.core.persistence.durable_runs import SubagentRunRow
from monkeybot.core.runtime.events import Error, TurnComplete, event_to_json
from monkeybot.core.subagents.subagent_proto import (
    SubagentEnvelope,
    resolve_subagent_script,
    spawn_subagent,
)

logger = logging.getLogger(__name__)

_SHUTDOWN_FAILURE_MESSAGE = "subagent run cancelled during worker shutdown"
_STALE_CLAIM_WARN_FRACTION = 0.8


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
class WorkerEnvSettings:
    poll_interval_s: float
    concurrency: int
    stale_claim_ms: int


def worker_env_settings() -> WorkerEnvSettings:
    """Read worker-pool tuning from environment once."""
    return WorkerEnvSettings(
        poll_interval_s=_env_float("MONKEYBOT_WORKER_POLL_INTERVAL_S", 2.0),
        concurrency=_env_int("MONKEYBOT_WORKER_CONCURRENCY", 1),
        stale_claim_ms=_env_int("MONKEYBOT_WORKER_STALE_CLAIM_MS", 600_000),
    )


@dataclass
class WorkerPoolHandle:
    """In-process worker pool task plus shared shutdown state."""

    task: asyncio.Task[None]
    backend: StorageBackend
    active_runs: set[str]
    worker_id: str


def resolve_worker_id() -> str:
    explicit = os.environ.get("MONKEYBOT_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return f"worker-{uuid.uuid4().hex[:12]}"


async def _still_owns_run(
    run_store: RunStore,
    run_id: str,
    worker_id: str,
    *,
    claimed_at: int | None = None,
) -> bool:
    row = await run_store.get_run(run_id)
    if row is None or row.status != "running" or row.worker_id != worker_id:
        return False
    if claimed_at is not None and row.claimed_at != claimed_at:
        return False
    return True


async def _fail_in_flight_runs(
    run_store: RunStore,
    run_ids: set[str],
    *,
    worker_id: str,
    message: str,
) -> None:
    for run_id in list(run_ids):
        try:
            if await _still_owns_run(run_store, run_id, worker_id):
                await run_store.record_failed(run_id, message)
        except Exception:
            logger.exception("failed to mark run_id=%s as failed during shutdown", run_id)
        finally:
            run_ids.discard(run_id)


async def _stale_claim_watchdog(
    run_id: str,
    claimed_at: int,
    stale_claim_ms: int,
) -> None:
    """Warn once when a run approaches the stale-claim reclaim window."""
    warn_after_ms = int(stale_claim_ms * _STALE_CLAIM_WARN_FRACTION)
    deadline_s = max(0.0, (claimed_at + warn_after_ms - int(time.time() * 1000)) / 1000.0)
    if deadline_s > 0:
        await asyncio.sleep(deadline_s)
    logger.warning(
        "subagent run_id=%s has been running for >=%.0f%% of MONKEYBOT_WORKER_STALE_CLAIM_MS "
        "(%d ms); if execution exceeds the full window another worker may reclaim and "
        "re-execute the run",
        run_id,
        _STALE_CLAIM_WARN_FRACTION * 100,
        stale_claim_ms,
    )


async def execute_claimed_run(
    run_store: RunStore,
    row: SubagentRunRow,
    *,
    script: Path,
    worker_id: str,
    stale_claim_ms: int = 600_000,
) -> None:
    """Run one claimed subagent row to completion and persist the outcome."""
    envelope = SubagentEnvelope.from_json(row.envelope_json)
    logger.debug(
        "worker executing %s",
        kv(run_id=row.run_id, worker_id=worker_id, parent_run_id=envelope.parent_run_id),
    )
    scratch = Path(row.scratch_dir)
    errors: list[str] = []
    last_event_json: str | None = None
    claimed_at = row.claimed_at
    if claimed_at is None:
        fresh = await run_store.get_run(row.run_id)
        if fresh is not None:
            claimed_at = fresh.claimed_at
    watchdog: asyncio.Task[None] | None = None
    if claimed_at is not None and stale_claim_ms > 0:
        watchdog = asyncio.create_task(
            _stale_claim_watchdog(row.run_id, claimed_at, stale_claim_ms),
            name=f"monkeybot-stale-watchdog-{row.run_id}",
        )

    async def _record_failed_if_owner(message: str) -> bool:
        if not await _still_owns_run(
            run_store,
            row.run_id,
            worker_id,
            claimed_at=claimed_at,
        ):
            logger.warning(
                "skipping failed outcome for run_id=%s worker_id=%s: claim lost",
                row.run_id,
                worker_id,
            )
            return False
        await run_store.record_failed(row.run_id, message)
        return True

    async def _record_completed_if_owner(result_json: str) -> bool:
        if not await _still_owns_run(
            run_store,
            row.run_id,
            worker_id,
            claimed_at=claimed_at,
        ):
            logger.warning(
                "skipping completed outcome for run_id=%s worker_id=%s: claim lost",
                row.run_id,
                worker_id,
            )
            return False
        await run_store.record_completed(row.run_id, result_json)
        return True

    extra_env: dict[str, str] | None = None
    if envelope.agent_md:
        extra_env = {"MONKEYBOT_SUBAGENT_AGENT_MD": envelope.agent_md}

    try:
        async for evt in spawn_subagent(
            str(script),
            envelope,
            scratch_dir=scratch,
            extra_env=extra_env,
        ):
            if isinstance(evt, Error):
                errors.append(evt.error)
            elif isinstance(evt, TurnComplete):
                last_event_json = event_to_json(evt)
    except asyncio.CancelledError:
        await _record_failed_if_owner(_SHUTDOWN_FAILURE_MESSAGE)
        raise
    except Exception as exc:
        logger.exception("worker failed executing run_id=%s", row.run_id)
        errors.append(str(exc))
    finally:
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    if errors:
        logger.debug(
            "worker finished %s",
            kv(run_id=row.run_id, worker_id=worker_id, outcome="failed"),
        )
        await _record_failed_if_owner("; ".join(errors))
        return

    if last_event_json is None:
        logger.debug(
            "worker finished %s",
            kv(run_id=row.run_id, worker_id=worker_id, outcome="failed_no_turn_complete"),
        )
        await _record_failed_if_owner("subagent produced no TurnComplete event")
        return

    logger.debug(
        "worker finished %s",
        kv(run_id=row.run_id, worker_id=worker_id, outcome="completed"),
    )
    await _record_completed_if_owner(last_event_json)


async def run_worker_loop(
    backend: StorageBackend,
    *,
    worker_id: str,
    script: Path,
    poll_interval_s: float = 2.0,
    concurrency: int = 1,
    stale_claim_ms: int = 600_000,
    active_runs: set[str] | None = None,
) -> None:
    """Poll ``pending_runs``, claim work atomically, and execute claimed rows."""
    run_store = backend.runs()
    sem = asyncio.Semaphore(max(1, concurrency))
    in_flight = active_runs if active_runs is not None else set()

    async def _try_claim_and_run(row: SubagentRunRow) -> None:
        if row.status != "pending":
            return
        async with sem:
            if not await run_store.claim(row.run_id, worker_id):
                return
            claimed_row = await run_store.get_run(row.run_id)
            if claimed_row is None:
                return
            logger.debug(
                "worker claimed %s",
                kv(run_id=row.run_id, worker_id=worker_id),
            )
            in_flight.add(row.run_id)
            try:
                await execute_claimed_run(
                    run_store,
                    claimed_row,
                    script=script,
                    worker_id=worker_id,
                    stale_claim_ms=stale_claim_ms,
                )
            except asyncio.CancelledError:
                await _fail_in_flight_runs(
                    run_store,
                    {row.run_id},
                    worker_id=worker_id,
                    message=_SHUTDOWN_FAILURE_MESSAGE,
                )
                raise
            finally:
                in_flight.discard(row.run_id)

    try:
        while True:
            try:
                reset_count = await run_store.reset_stale_claims(stale_claim_ms)
                if reset_count:
                    logger.info(
                        "reset stale subagent claims %s",
                        kv(count=reset_count, worker_id=worker_id),
                    )
                pending = await run_store.pending_runs()
                tasks = [_try_claim_and_run(row) for row in pending if row.status == "pending"]
                if tasks:
                    await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker poll loop error worker_id=%s", worker_id)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        await _fail_in_flight_runs(
            run_store,
            in_flight,
            worker_id=worker_id,
            message=_SHUTDOWN_FAILURE_MESSAGE,
        )
        raise


def start_worker_pool_background(
    backend: StorageBackend,
    *,
    worker_id: str | None = None,
    script: Path | None = None,
) -> WorkerPoolHandle:
    """Start the poll-and-claim loop as a background asyncio task.

    Development-only entry point: the loop runs on the caller's event loop (e.g. the
    gateway process), so subagent execution competes with whatever else that loop
    serves (SSE streams) and there is no backpressure between them. For production,
    run standalone worker processes via ``python -m monkeybot.subagents.worker``
    (see :func:`run_worker_main`), which scale independently of the gateway.
    """
    wid = worker_id or resolve_worker_id()
    resolved_script = script or resolve_subagent_script()
    settings = worker_env_settings()
    active_runs: set[str] = set()
    logger.info(
        "starting subagent worker pool worker_id=%s script=%s concurrency=%d "
        "stale_claim_ms=%d (MONKEYBOT_WORKER_STALE_CLAIM_MS; runs without heartbeat "
        "may be reclaimed and re-executed after this window)",
        wid,
        resolved_script,
        settings.concurrency,
        settings.stale_claim_ms,
    )
    task = asyncio.create_task(
        run_worker_loop(
            backend,
            worker_id=wid,
            script=resolved_script,
            poll_interval_s=settings.poll_interval_s,
            concurrency=settings.concurrency,
            stale_claim_ms=settings.stale_claim_ms,
            active_runs=active_runs,
        ),
        name=f"monkeybot-worker-{wid}",
    )
    return WorkerPoolHandle(
        task=task,
        backend=backend,
        active_runs=active_runs,
        worker_id=wid,
    )


async def shutdown_worker_pool(handle: WorkerPoolHandle) -> None:
    """Cancel an in-process worker pool and fail any still-running claimed rows."""
    handle.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await handle.task
    await _fail_in_flight_runs(
        handle.backend.runs(),
        handle.active_runs,
        worker_id=handle.worker_id,
        message=_SHUTDOWN_FAILURE_MESSAGE,
    )


async def run_worker_main() -> None:
    """CLI entry: open storage backend and run the worker loop until cancelled."""
    from monkeybot.core.config.settings import auto_schema_enabled_from_config
    from monkeybot.core.persistence.backends import create_storage_backend

    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    settings = worker_env_settings()
    logger.info(
        "subagent worker starting stale_claim_ms=%d (MONKEYBOT_WORKER_STALE_CLAIM_MS)",
        settings.stale_claim_ms,
    )
    backend = create_storage_backend(db_url)
    await backend.open(run_schema=auto_schema_enabled_from_config())
    try:
        await run_worker_loop(
            backend,
            worker_id=resolve_worker_id(),
            script=resolve_subagent_script(),
            poll_interval_s=settings.poll_interval_s,
            concurrency=settings.concurrency,
            stale_claim_ms=settings.stale_claim_ms,
        )
    finally:
        await backend.close()
