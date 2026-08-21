"""Poll-and-claim worker pool for queued subagent runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from monkeybot.core.config.settings import auto_schema_enabled_from_config, get_subagent_settings
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import RunStore, StorageBackend, create_storage_backend
from monkeybot.core.persistence.durable_runs import SubagentRunRow
from monkeybot.core.runtime.events import Error, TurnComplete, event_to_json
from monkeybot.core.subagents.subagent_proto import (
    SUBAGENT_STDOUT_LINE_LIMIT,
    SubagentEnvelope,
    resolve_subagent_script,
    spawn_subagent,
)
from monkeybot.core.subprocess_groups import (
    SUPPORTS_PROCESS_GROUPS,
    process_group_id,
    stop_subagent_process,
)

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
    worker_id: str


def resolve_worker_id() -> str:
    explicit = os.environ.get("MONKEYBOT_WORKER_ID", "").strip()
    if explicit:
        return explicit
    return f"worker-{uuid.uuid4().hex[:12]}"


async def _fail_in_flight_runs(
    run_store: RunStore,
    run_ids: set[str],
    *,
    worker_id: str,
    message: str,
) -> None:
    for run_id in list(run_ids):
        try:
            await run_store.record_failed(run_id, message, worker_id=worker_id)
        except Exception:
            logger.exception("failed to mark run_id=%s as failed during shutdown", run_id)
        finally:
            run_ids.discard(run_id)


def _process_identity(pid: int) -> str | None:
    """Return a stable identity token for ``pid``, or None if the process is gone.

    Used to avoid SIGKILL/killpg against a reused PID after the original
    subagent exits. Prefer Linux ``/proc`` starttime (+ boot id); fall back to
    ``ps`` start time on other Unix platforms.
    """
    if pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            raw = proc_stat.read_text(encoding="utf-8")
            close = raw.rfind(")")
            if close < 0:
                return None
            fields = raw[close + 1 :].split()
            if len(fields) < 20:
                return None
            starttime = fields[19]
            boot_id = ""
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                pass
            return f"linux:{boot_id}:{starttime}"
        except OSError:
            return None
    if not SUPPORTS_PROCESS_GROUPS:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    token = (completed.stdout or "").strip()
    if not token or completed.returncode != 0:
        return None
    return f"ps:{token}"


def _write_subagent_pid(scratch: Path, pid: int) -> None:
    identity = _process_identity(pid)
    if identity is None:
        logger.warning(
            "skipping subagent.pid write for pid=%s under %s: process identity unavailable",
            pid,
            scratch,
        )
        return
    try:
        (scratch / "subagent.pid").write_text(f"{pid}\n{identity}\n", encoding="utf-8")
    except OSError:
        logger.warning("failed to write subagent.pid under %s", scratch)


def _read_subagent_pid(scratch: Path) -> tuple[int, str] | None:
    pid_path = scratch / "subagent.pid"
    try:
        lines = [
            line.strip()
            for line in pid_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    identity = lines[1]
    if pid <= 0 or not identity:
        return None
    return pid, identity


def _kill_scratch_subagent(scratch: Path) -> None:
    """Best-effort kill of a reclaimed run's child process (from scratch/subagent.pid)."""
    recorded = _read_subagent_pid(scratch)
    if recorded is None:
        return
    pid, expected_identity = recorded
    current = _process_identity(pid)
    if current is not None and current != expected_identity:
        logger.warning(
            "skipping reclaim kill for pid=%s under %s: identity mismatch "
            "(possible PID reuse)",
            pid,
            scratch,
        )
        return
    # Leader may already be gone; killpg still reaches orphaned group members.
    if SUPPORTS_PROCESS_GROUPS:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.warning("permission denied killing reclaimed subagent pid=%s", pid)


async def _claim_heartbeat(
    run_store: RunStore,
    run_id: str,
    worker_id: str,
    stale_claim_ms: int,
) -> None:
    """Renew claimed_at while the run executes so live workers are not reclaimed."""
    interval_s = max(1.0, (stale_claim_ms / 1000.0) / 4.0)
    while True:
        await asyncio.sleep(interval_s)
        try:
            ok = await run_store.renew_claim(run_id, worker_id)
        except Exception:
            logger.exception(
                "claim heartbeat failed %s",
                kv(run_id=run_id, worker_id=worker_id),
            )
            return
        if not ok:
            logger.warning(
                "claim heartbeat lost %s",
                kv(run_id=run_id, worker_id=worker_id),
            )
            return


async def execute_claimed_run(
    run_store: RunStore,
    row: SubagentRunRow,
    *,
    script: Path,
    worker_id: str,
    stale_claim_ms: int = 600_000,
    timeout_sec: float | None = None,
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
    heartbeat: asyncio.Task[None] | None = None
    if stale_claim_ms > 0:
        heartbeat = asyncio.create_task(
            _claim_heartbeat(run_store, row.run_id, worker_id, stale_claim_ms),
            name=f"monkeybot-claim-heartbeat-{row.run_id}",
        )

    async def _record_failed_if_owner(message: str) -> bool:
        ok = await run_store.record_failed(row.run_id, message, worker_id=worker_id)
        if not ok:
            logger.warning(
                "skipping failed outcome for run_id=%s worker_id=%s: claim lost",
                row.run_id,
                worker_id,
            )
        return ok

    async def _record_completed_if_owner(result_json: str) -> bool:
        ok = await run_store.record_completed(
            row.run_id, result_json, worker_id=worker_id
        )
        if not ok:
            logger.warning(
                "skipping completed outcome for run_id=%s worker_id=%s: claim lost",
                row.run_id,
                worker_id,
            )
        return ok

    budget = get_subagent_settings().timeout_sec if timeout_sec is None else float(timeout_sec)
    proc_holder: list[asyncio.subprocess.Process | None] = [None]
    pgid_holder: list[int | None] = [None]

    async def _subprocess_exec(*cmd: str | bytes) -> asyncio.subprocess.Process:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        # start_new_session so timeout/cancel can kill the whole process group
        # (descendants would otherwise keep mutating the workspace).
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=SUBAGENT_STDOUT_LINE_LIMIT,
            start_new_session=SUPPORTS_PROCESS_GROUPS,
        )
        proc_holder[0] = proc
        pgid_holder[0] = process_group_id(proc.pid)
        if proc.pid is not None:
            _write_subagent_pid(scratch, proc.pid)
        return proc

    async def _consume() -> None:
        nonlocal last_event_json
        async for evt in spawn_subagent(
            str(script),
            envelope,
            scratch_dir=scratch,
            subprocess_exec=_subprocess_exec,
        ):
            if isinstance(evt, Error):
                errors.append(evt.error)
            elif isinstance(evt, TurnComplete):
                last_event_json = event_to_json(evt)

    consume_task = asyncio.create_task(_consume())
    try:
        try:
            await asyncio.wait_for(consume_task, timeout=budget)
        except TimeoutError:
            progress = scratch / "progress.jsonl"
            msg = (
                f"exit_reason=timeout: subagent exceeded {budget:g}s; "
                f"inspect {progress}"
            )
            logger.warning(
                "worker subagent timeout %s",
                kv(run_id=row.run_id, timeout_sec=budget, progress=str(progress)),
            )
            errors.append(msg)
            await stop_subagent_process(proc_holder[0], pgid=pgid_holder[0])
            if not consume_task.done():
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task
        except asyncio.CancelledError:
            await stop_subagent_process(proc_holder[0], pgid=pgid_holder[0])
            if not consume_task.done():
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task
            await _record_failed_if_owner(_SHUTDOWN_FAILURE_MESSAGE)
            raise
        except Exception as exc:
            logger.exception("worker failed executing run_id=%s", row.run_id)
            errors.append(str(exc))
            await stop_subagent_process(proc_holder[0], pgid=pgid_holder[0])
            if not consume_task.done():
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            # renew_claim failures must not escape and skip outcome recording.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat

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
                stale_rows = await run_store.list_stale_claims(stale_claim_ms)
                reset_count = 0
                for stale in stale_rows:
                    # Reset first; only kill if we won the reclaim so a renewed
                    # heartbeat cannot leave a live run killed while still claimed.
                    if not await run_store.reset_stale_claim(
                        stale.run_id,
                        stale_claim_ms,
                        worker_id=stale.worker_id,
                    ):
                        continue
                    reset_count += 1
                    _kill_scratch_subagent(Path(stale.scratch_dir))
                    logger.warning(
                        "reclaiming stale subagent claim %s",
                        kv(
                            run_id=stale.run_id,
                            worker_id=stale.worker_id or "",
                            exit_reason="reclaimed",
                        ),
                    )
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
        "stale_claim_ms=%d (MONKEYBOT_WORKER_STALE_CLAIM_MS; live runs heartbeat the "
        "claim lease; stale runs are killed via scratch/subagent.pid then requeued)",
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
    db_url = os.environ.get("DB_URL", "sqlite:///data/monkeybot.db")
    settings = worker_env_settings()
    logger.info(
        "subagent worker starting stale_claim_ms=%d (MONKEYBOT_WORKER_STALE_CLAIM_MS)",
        settings.stale_claim_ms,
    )
    # No agent_scope: this worker only claims/records subagent_runs rows via
    # .runs(), never .history() — conversation_history's agent-scope
    # isolation (PR #179) doesn't apply to what this process touches.
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
