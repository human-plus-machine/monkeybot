# Code Spec: Story 2 — Scheduler

**Story:** `.monkeymode/monkeybot-v2-e3/user_stories.md` — Story 2  
**Design Reference:** `1a-discovery.md` §ADR-001/ADR-002, `1b-contracts.md` §core/scheduler.py  
**Date:** 2026-05-13

## Implementation Summary

- **Files to Create:** 2 (`core/scheduler.py`, `tests/unit/test_scheduler.py`)
- **Files to Modify:** 2 (`cli.py` — `_serve_async` only; `bots/example-bot/config.yaml` — add scheduler section)
- **Estimated Complexity:** M

## Codebase Conventions

Same as Story 1 — see conventions section there. Key references:
- `src/monkeybot/core/history.py` — aiosqlite lazy init pattern
- `src/monkeybot/core/loop.py` — asyncio.Task lifecycle
- `src/monkeybot/cli.py` — `_serve_async` pattern to wire new service

---

## Task 1: Create `core/scheduler.py`

**Files**: `src/monkeybot/core/scheduler.py` (create)

### Dataclasses and types

```python
from __future__ import annotations
import asyncio, importlib, logging, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import aiosqlite

log = logging.getLogger("monkeybot.scheduler")
_croniter_warned = False  # module-level, one-time warning flag

@dataclass
class JobConfig:
    name: str
    cron: str
    callable: str   # "module.path:function"
    enabled: bool = True
```

### `Scheduler` class

```python
class Scheduler:
    def __init__(self, db_path: str, jobs: list[JobConfig], poll_interval: int = 30) -> None:
        self._db_path = db_path
        self._jobs = [j for j in jobs if j.enabled]
        self._poll_interval = poll_interval
        self._callables: dict[str, Any] = {}   # pre-loaded at start()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def _init_db(self) -> None: ...
    async def _tick(self) -> None: ...
    def _load_callable(self, job: JobConfig) -> Any | None: ...
    def _next_run(self, cron: str, after: datetime) -> datetime: ...
```

### `start()` algorithm

1. Guard: `if self._task is not None: return`
2. Call `await self._init_db()` — creates `job_runs` table, initializes `next_run = now_ms` for any job not yet in table
3. Pre-load callables: for each job, call `self._load_callable(job)` — on `ImportError` / `AttributeError`, log WARNING and skip (don't add to `_callables`)
4. Spawn: `self._task = asyncio.create_task(self._poll_loop())`
5. Log INFO: `"scheduler started poll_interval=%ds jobs=%d"`

### `_poll_loop()` — the inner coroutine

```python
async def _poll_loop(self) -> None:
    log.info("scheduler started poll_interval=%ds jobs=%d", self._poll_interval, len(self._jobs))
    while True:
        try:
            await self._tick()
        except Exception:
            log.exception("scheduler _tick failed")
        await asyncio.sleep(self._poll_interval)
```

### `stop()` algorithm

1. `if self._task is None: return`
2. `self._task.cancel()`
3. `try: await self._task` / `except asyncio.CancelledError: pass`
4. `self._task = None`
5. Log INFO: `"scheduler stopped"`

### `_init_db()` algorithm

```sql
CREATE TABLE IF NOT EXISTS job_runs (
    job_name  TEXT    PRIMARY KEY,
    last_run  INTEGER,
    next_run  INTEGER NOT NULL
)
```

After creating table: for each job in `self._jobs`, `INSERT OR IGNORE INTO job_runs (job_name, next_run) VALUES (?, ?)` with `next_run = int(time.time() * 1000)` (i.e. treat as overdue immediately so it fires on first tick — this satisfies "NULL next_run fires immediately" semantically).

Set WAL + synchronous=NORMAL pragma (same as history.py).

### `_tick()` algorithm

```
1. now_ms = int(time.time() * 1000)
2. SELECT job_name, next_run FROM job_runs WHERE next_run <= ?  [now_ms]
3. For each overdue row:
   a. callable_ = self._callables.get(row.job_name)
   b. if callable_ is None: log WARNING, advance next_run, continue
   c. try:
        await callable_()     # must be a coroutine function
        log.info("job fired name=%s", row.job_name)
      except Exception:
        log.exception("job failed name=%s", row.job_name)
   d. (regardless of success/failure) compute new_next_run via _next_run(job.cron, datetime.now(tz=utc))
   e. UPDATE job_runs SET last_run=?, next_run=? WHERE job_name=?
```

### `_next_run()` algorithm

```python
def _next_run(self, cron: str, after: datetime) -> datetime:
    global _croniter_warned
    try:
        from croniter import croniter  # noqa: PLC0415
        return croniter(cron, after).get_next(datetime)
    except ImportError:
        if not _croniter_warned:
            log.warning("croniter not available — using +1 hour fallback for all cron jobs")
            _croniter_warned = True
        from datetime import timedelta
        return after + timedelta(hours=1)
```

### `_load_callable()` algorithm

```python
def _load_callable(self, job: JobConfig) -> Any | None:
    try:
        module_path, fn_name = job.callable.rsplit(":", 1)
        module = importlib.import_module(module_path)
        return getattr(module, fn_name)
    except (ImportError, AttributeError, ValueError) as exc:
        log.warning("job callable not importable name=%s callable=%s error=%s",
                    job.name, job.callable, exc)
        return None
```

---

## Task 2: Wire Scheduler into `cli.py` `_serve_async`

**Files**: `src/monkeybot/cli.py` (modify — `_serve_async` only)

**Add import:**
```python
from monkeybot.core.scheduler import JobConfig, Scheduler
```

**In `_serve_async`, after `history = ConversationHistory(...)` / `await history.init()`:**

```python
# Build Scheduler if jobs defined in config
scheduler: Scheduler | None = None
scheduler_config = bot_config.get("scheduler")
if isinstance(scheduler_config, dict) and scheduler_config.get("jobs"):
    raw_jobs = scheduler_config["jobs"]
    poll_interval = int(scheduler_config.get("poll_interval", 30))
    jobs = [
        JobConfig(
            name=name,
            cron=str(cfg.get("cron", "0 * * * *")),
            callable=str(cfg.get("callable", "")),
            enabled=bool(cfg.get("enabled", True)),
        )
        for name, cfg in raw_jobs.items()
        if isinstance(cfg, dict) and cfg.get("callable")
    ]
    db_path = db_url.removeprefix("sqlite:///")
    scheduler = Scheduler(db_path=db_path, jobs=jobs, poll_interval=poll_interval)
```

**Wrap `await server.serve()` in try/finally:**
```python
if scheduler is not None:
    await scheduler.start()
try:
    await server.serve()
finally:
    if scheduler is not None:
        await scheduler.stop()
```

---

## Task 3: Update `bots/example-bot/config.yaml`

Add commented-out scheduler section at the bottom:

```yaml
# Scheduler — optional, long-running deployments only.
# Requires monkeybot[scheduler] for cron expressions (pip install monkeybot[scheduler]).
# NOTE: incompatible with serverless / ephemeral compute (Lambda, AgentCore, Agent Engine).
# For serverless, use EventBridge / Cloud Scheduler to POST to /webhook instead.
#
# scheduler:
#   poll_interval: 30   # seconds
#   jobs:
#     daily-summary:
#       cron: "0 9 * * *"        # every day at 09:00 UTC
#       callable: "bots.example_bot.jobs:daily_summary"
#       enabled: false           # set to true to activate
```

---

## Task 4: Write `tests/unit/test_scheduler.py`

**Files**: `tests/unit/test_scheduler.py` (create)  
**Pattern**: pytest-asyncio, `tmp_path` fixture for DB, `unittest.mock.AsyncMock` for job callables

**Fixtures:**
```python
@pytest.fixture
def db_path(tmp_path): return str(tmp_path / "sched.db")

def make_scheduler(db_path, jobs=None, poll_interval=1):
    return Scheduler(db_path=db_path, jobs=jobs or [], poll_interval=poll_interval)
```

**Test cases:**

- `test_tick_fires_overdue_job`: Create `Scheduler` with one job. After `await scheduler.start()`, wait >1s, check callable was called. (Or call `_tick()` directly after `_init_db()` and pre-loading callables with a known mock.)

  **Preferred approach to avoid timing flakiness:** call `await scheduler._init_db()` + set `scheduler._callables = {"test-job": mock_fn}` + manually set `next_run` to past in DB + call `await scheduler._tick()` + assert mock called.

- `test_tick_skips_future_job`: Set `next_run` to `now + 10000ms`, call `_tick()`, assert callable NOT called.

- `test_tick_updates_next_run_after_fire`: Before tick: `next_run = now - 1`. After `_tick()`: query DB → `last_run` is not NULL, `next_run > now`.

- `test_tick_job_failure_continues_and_advances_next_run`: Mock callable raises `RuntimeError`. Call `_tick()`. Assert no exception propagated. Assert `next_run` still advanced in DB.

- `test_croniter_fallback`: Patch croniter import to raise `ImportError`. Call `scheduler._next_run("0 9 * * *", datetime.now(tz=timezone.utc))`. Assert result is approximately `now + 1 hour`.

- `test_scheduler_start_stop`: `await scheduler.start()` → `_task` is not None. `await scheduler.stop()` → `_task` is None. No exception.

- `test_null_next_run_fires_immediately`: `INSERT OR IGNORE` sets `next_run = now_ms` at init. After `start()`, on first `_tick()`, job fires. (Verify via direct `_tick()` call as above.)

---

## Dependency Graph

Tasks 1 → 2 (cli.py wiring depends on Scheduler class existing)  
Tasks 1 → 4 (tests depend on Scheduler existing)  
Task 3 is independent (config.yaml edit)

---

## Final Verification

- [ ] `Scheduler.start()` is idempotent (double-call safe)
- [ ] `Scheduler.stop()` cancels task cleanly without `CancelledError` propagation
- [ ] Job with `next_run` in past fires on first `_tick()`
- [ ] Job callable failure is logged; `next_run` still advances; loop continues
- [ ] `croniter` unavailable → `+1 hour` fallback with one-time WARNING
- [ ] `scheduler.jobs` absent in config → `Scheduler` not instantiated, `job_runs` table not created
- [ ] `_serve_async` starts scheduler before serving, stops it in `finally`
- [ ] All 7 unit tests pass
- [ ] `ruff check src/` and `mypy --strict src/` clean
