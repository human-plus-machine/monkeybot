"""Poll-and-claim worker pool for queued subagent runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from monkeybot.core.persistence.backends import RunStore, StorageBackend
from monkeybot.core.persistence.durable_runs import SubagentRunRow
from monkeybot.core.runtime.events import Error, TurnComplete, event_to_json
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope, spawn_subagent

logger = logging.getLogger(__name__)

_SHUTDOWN_FAILURE_MESSAGE = "subagent run cancelled during worker shutdown"


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


def resolve_worker_id() -> str:
    explicit = os.environ.get("MONKEYBOT_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return f"worker-{uuid.uuid4().hex[:12]}"


def resolve_subagent_script() -> Path:
    return Path(
        os.environ.get(
            "MONKEYBOT_SUBAGENT_SCRIPT",
            str(Path(__file__).resolve().parent / "subagent_worker.py"),
        )
    ).resolve()


async def _fail_in_flight_runs(
    run_store: RunStore,
    run_ids: set[str],
    *,
    message: str,
) -> None:
    for run_id in list(run_ids):
        try:
            row = await run_store.get_run(run_id)
            if row is not None and row.status == "running":
                await run_store.record_failed(run_id, message)
        except Exception:
            logger.exception("failed to mark run_id=%s as failed during shutdown", run_id)
        finally:
            run_ids.discard(run_id)


async def execute_claimed_run(
    run_store: RunStore,
    row: SubagentRunRow,
    *,
    script: Path,
) -> None:
    """Run one claimed subagent row to completion and persist the outcome."""
    envelope = SubagentEnvelope.from_json(row.envelope_json)
    scratch = Path(row.scratch_dir)
    errors: list[str] = []
    last_event_json: str | None = None

    try:
        async for evt in spawn_subagent(
            str(script),
            envelope,
            scratch_dir=scratch,
        ):
            if isinstance(evt, Error):
                errors.append(evt.error)
            elif isinstance(evt, TurnComplete):
                last_event_json = event_to_json(evt)
    except asyncio.CancelledError:
        await run_store.record_failed(row.run_id, _SHUTDOWN_FAILURE_MESSAGE)
        raise
    except Exception as exc:
        logger.exception("worker failed executing run_id=%s", row.run_id)
        errors.append(str(exc))

    if errors:
        await run_store.record_failed(row.run_id, "; ".join(errors))
        return

    if last_event_json is None:
        await run_store.record_failed(row.run_id, "subagent produced no TurnComplete event")
        return

    await run_store.record_completed(row.run_id, last_event_json)


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
            in_flight.add(row.run_id)
            try:
                await execute_claimed_run(run_store, row, script=script)
            except asyncio.CancelledError:
                await _fail_in_flight_runs(
                    run_store,
                    {row.run_id},
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
                    logger.info("reset %d stale subagent claims", reset_count)
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
        "starting subagent worker pool worker_id=%s script=%s concurrency=%d",
        wid,
        resolved_script,
        settings.concurrency,
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
    return WorkerPoolHandle(task=task, backend=backend, active_runs=active_runs)


async def shutdown_worker_pool(handle: WorkerPoolHandle) -> None:
    """Cancel an in-process worker pool and fail any still-running claimed rows."""
    handle.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await handle.task
    await _fail_in_flight_runs(
        handle.backend.runs(),
        handle.active_runs,
        message=_SHUTDOWN_FAILURE_MESSAGE,
    )


async def run_worker_main() -> None:
    """CLI entry: open storage backend and run the worker loop until cancelled."""
    from monkeybot.core.persistence.backends import create_storage_backend

    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    settings = worker_env_settings()
    backend = create_storage_backend(db_url)
    await backend.open()
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
