from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger("monkeybot.scheduler")
_croniter_warned = False  # module-level one-time warning flag

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS job_runs (
    job_name  TEXT    PRIMARY KEY,
    last_run  INTEGER,
    next_run  INTEGER NOT NULL
)"""


@dataclass
class JobConfig:
    """Configuration for a single scheduled job."""

    name: str  # unique key, matches job_runs.job_name
    cron: str  # e.g. "0 9 * * *"
    callable: str  # dotted "module.path:function"
    enabled: bool = True


class Scheduler:
    """Cron-style async job scheduler backed by SQLite."""

    def __init__(
        self,
        db_path: str,
        jobs: list[JobConfig],
        poll_interval: int = 30,
    ) -> None:
        self._db_path = db_path
        self._jobs = [j for j in jobs if j.enabled]
        self._poll_interval = poll_interval
        self._callables: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None

    async def _init_db(self) -> None:
        """Create job_runs table and seed initial rows for all configured jobs."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_CREATE_TABLE)
            now_ms = int(time.time() * 1000)
            for job in self._jobs:
                await db.execute(
                    "INSERT OR IGNORE INTO job_runs (job_name, next_run) VALUES (?, ?)",
                    (job.name, now_ms),
                )
            await db.commit()

    def _load_callable(self, job: JobConfig) -> Any | None:
        """Import and return the callable for *job*, or None on failure."""
        try:
            module_path, fn_name = job.callable.rsplit(":", 1)
            module = importlib.import_module(module_path)
            return getattr(module, fn_name)
        except (ImportError, AttributeError, ValueError) as exc:
            log.warning(
                "job callable not importable name=%s callable=%s error=%s",
                job.name,
                job.callable,
                exc,
            )
            return None

    def _next_run(self, cron: str, after: datetime) -> datetime:
        """Return the next datetime to run *cron* after *after*.

        Falls back to +1 hour if croniter is not installed.
        """
        global _croniter_warned
        try:
            from croniter import croniter  # type: ignore[import-untyped]  # noqa: PLC0415

            return croniter(cron, after).get_next(datetime)  # type: ignore[no-any-return]
        except ImportError:
            if not _croniter_warned:
                log.warning(
                    "croniter not available — using +1 hour fallback for all cron jobs"
                )
                _croniter_warned = True
            return after + timedelta(hours=1)

    async def start(self) -> None:
        """Initialise DB, pre-load callables, and begin the poll loop."""
        if self._task is not None:
            return
        await self._init_db()
        for job in self._jobs:
            fn = self._load_callable(job)
            if fn is not None:
                self._callables[job.name] = fn
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        log.info(
            "scheduler started poll_interval=%ds jobs=%d",
            self._poll_interval,
            len(self._jobs),
        )
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler _tick failed")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        """Fire all overdue jobs and advance their next_run timestamps."""
        now_ms = int(time.time() * 1000)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT job_name FROM job_runs WHERE next_run <= ?",
                (now_ms,),
            ) as cur:
                rows = await cur.fetchall()

            job_map = {j.name: j for j in self._jobs}

            for (job_name,) in rows:
                job = job_map.get(job_name)
                if job is None:
                    continue

                callable_ = self._callables.get(job_name)
                new_next_run = self._next_run(job.cron, datetime.now(tz=UTC))

                if callable_ is None:
                    log.warning("job has no callable, skipping name=%s", job_name)
                else:
                    try:
                        await callable_()
                        log.info("job fired name=%s", job_name)
                    except Exception:
                        log.exception("job failed name=%s", job_name)

                await db.execute(
                    "UPDATE job_runs SET last_run=?, next_run=? WHERE job_name=?",
                    (now_ms, int(new_next_run.timestamp() * 1000), job_name),
                )

            await db.commit()

    async def stop(self) -> None:
        """Cancel the poll loop and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        log.info("scheduler stopped")
