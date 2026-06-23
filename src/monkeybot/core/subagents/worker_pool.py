"""Poll-and-claim worker pool for queued subagent runs."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

from monkeybot.core.persistence.backends import RunStore, StorageBackend
from monkeybot.core.persistence.durable_runs import SubagentRunRow
from monkeybot.core.runtime.events import Error, TurnComplete, event_to_json
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope, spawn_subagent

logger = logging.getLogger(__name__)


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
) -> None:
    """Poll ``pending_runs``, claim work atomically, and execute claimed rows."""
    run_store = backend.runs()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _try_claim_and_run(row: SubagentRunRow) -> None:
        if row.status != "pending":
            return
        if not await run_store.claim(row.run_id, worker_id):
            return
        async with sem:
            await execute_claimed_run(run_store, row, script=script)

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


def start_worker_pool_background(
    backend: StorageBackend,
    *,
    worker_id: str | None = None,
    script: Path | None = None,
) -> asyncio.Task[None]:
    """Start the poll-and-claim loop as a background asyncio task."""
    wid = worker_id or resolve_worker_id()
    resolved_script = script or resolve_subagent_script()
    poll_interval_s = _env_float("MONKEYBOT_WORKER_POLL_INTERVAL_S", 2.0)
    concurrency = _env_int("MONKEYBOT_WORKER_CONCURRENCY", 1)
    stale_claim_ms = _env_int("MONKEYBOT_WORKER_STALE_CLAIM_MS", 600_000)
    logger.info(
        "starting subagent worker pool worker_id=%s script=%s concurrency=%d",
        wid,
        resolved_script,
        concurrency,
    )
    return asyncio.create_task(
        run_worker_loop(
            backend,
            worker_id=wid,
            script=resolved_script,
            poll_interval_s=poll_interval_s,
            concurrency=concurrency,
            stale_claim_ms=stale_claim_ms,
        ),
        name=f"monkeybot-worker-{wid}",
    )


async def run_worker_main() -> None:
    """CLI entry: open storage backend and run the worker loop until cancelled."""
    from monkeybot.core.persistence.backends import create_storage_backend

    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    backend = create_storage_backend(db_url)
    await backend.open()
    try:
        await run_worker_loop(
            backend,
            worker_id=resolve_worker_id(),
            script=resolve_subagent_script(),
            poll_interval_s=_env_float("MONKEYBOT_WORKER_POLL_INTERVAL_S", 2.0),
            concurrency=_env_int("MONKEYBOT_WORKER_CONCURRENCY", 1),
            stale_claim_ms=_env_int("MONKEYBOT_WORKER_STALE_CLAIM_MS", 600_000),
        )
    finally:
        await backend.close()
