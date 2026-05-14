# User Stories — monkeybot-v2-e3
## E3: Observability, Scheduling & Cost Tracking

**Phase:** 2 — User Stories  
**Date:** 2026-05-13  
**Source PRT:** `.prt/monkeybot-v2/epics/e3-observability-scheduling.md`

---

## Parallelization Plan

E3 has **two independent components** with zero file overlap:

| Story | Component | Files |
|---|---|---|
| Story 1 | Usage Recording & CLI | `core/usage.py`, `cli.py` (usage command only), `tests/unit/test_usage.py` |
| Story 2 | Scheduler | `core/scheduler.py`, `cli.py` (serve wiring only), `bots/example-bot/config.yaml`, `tests/unit/test_scheduler.py` |

`cli.py` is the only shared file — but each story modifies a different, non-overlapping section (Story 1: adds `usage` command; Story 2: adds scheduler start to `serve`). Phase 6 integration merges these two `cli.py` changes cleanly.

**Both stories start immediately, in parallel.**

---

## Story 1: Usage Recording & CLI

**Type:** Feature  
**Priority:** Should  
**Size:** S (1–2 days)  
**Dependencies:** NONE (independent of Story 2)

### Description

As a bot developer,  
I want to inspect per-turn cost and token usage via `monkeybot usage`,  
So that I can monitor spend without a third-party observability tool.

### Technical Context

- **Affected modules:** `monkeybot.core.usage` (new), `monkeybot.cli` (add `usage` command)
- **Design reference:** `1a-discovery.md` "Core Data Model → turn_usage table", `1b-contracts.md` "core/usage.py contracts"
- **Key files to create:**
  - `src/monkeybot/core/usage.py` — `UsageSummary` dataclass, `record_usage()`, `get_usage_summary()`
  - `tests/unit/test_usage.py` — 5 unit tests against real SQLite `:memory:`
- **Key files to modify:**
  - `src/monkeybot/cli.py` — add `usage` command to `main` click group; wire `record_usage()` call into `_run_async` and `_serve_async` after consuming `TurnComplete` from the event stream
- **Patterns to follow:**
  - `src/monkeybot/core/history.py` — same aiosqlite open-per-call pattern, WAL mode, lazy table init
  - `src/monkeybot/cli.py` — existing `@main.command()` pattern for `run` and `serve`
- **Dependencies:** NONE — uses `TurnComplete` (E1, already exists), `aiosqlite` (already in deps), `click` (already in deps)

### Integration Contracts

**Types defined by this story:**

```python
# src/monkeybot/core/usage.py
from dataclasses import dataclass

@dataclass
class UsageSummary:
    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_cost_usd: float
    avg_latency_ms: float
    since_hours: float

async def record_usage(
    db_path: str,
    session_id: str,
    event: TurnComplete,
) -> None:
    """Insert one row into turn_usage. Idempotent on run_id."""

async def get_usage_summary(
    db_path: str,
    since_hours: float,
) -> UsageSummary:
    """Aggregate turn_usage rows since N hours ago."""
```

**Used by Story 2:** No direct dependency. Story 2's `Scheduler` does not call `record_usage`.  
**Used by cli.py wiring (Phase 6 integration):** `record_usage` is called by the gateway layer after each `TurnComplete` event.

### `turn_usage` table schema

```sql
CREATE TABLE IF NOT EXISTS turn_usage (
    id            TEXT    PRIMARY KEY,           -- ULID
    run_id        TEXT    NOT NULL UNIQUE,        -- from TurnComplete.run_id
    session_id    TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL               -- Unix ms
);
CREATE INDEX IF NOT EXISTS idx_turn_usage_created ON turn_usage(created_at);
```

### Acceptance Criteria

- [ ] **Given** a `TurnComplete` event with `run_id="01HXY"`, `input_tokens=100`, **When** `record_usage()` is called, **Then** one row exists in `turn_usage` with matching `run_id` and `input_tokens=100`
- [ ] **Given** `record_usage()` called twice with the same `run_id`, **When** both calls complete, **Then** exactly 1 row exists (idempotent via `INSERT OR IGNORE`)
- [ ] **Given** no rows in `turn_usage`, **When** `get_usage_summary(since_hours=24)` is called, **Then** returns `UsageSummary` with all fields = 0
- [ ] **Given** 3 rows in `turn_usage` (2 within the `since` window, 1 older), **When** `get_usage_summary(since_hours=24)` is called, **Then** returns sums/avg for the 2 in-window rows only
- [ ] **Given** `turn_usage` table doesn't exist yet, **When** `record_usage()` is first called, **Then** table is created and row is inserted (lazy init)
- [ ] **Given** no usage data, **When** `monkeybot usage --since 24` is run, **Then** stdout prints `No usage data found.` and exits 0
- [ ] **Given** ≥1 turn recorded, **When** `monkeybot usage --since 24` is run, **Then** stdout prints formatted summary with correct values
- [ ] `ruff check` and `mypy --strict` pass on `core/usage.py`

### CLI Output Contract

```
Usage summary (last 24h)
────────────────────────────────
Turns             :     42
Input tokens      : 18,432
Output tokens     :  6,114
Cached tokens     :  2,048
Total cost (USD)  :  $0.0231
Avg latency (ms)  :    843
```

### Implementation Notes

- Open a fresh `aiosqlite` connection per call (same as `ConversationHistory`) — no connection pool
- `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL` on first connection (same as history.py)
- `cost_usd` will be 0.0 for all current turns (neither provider calculates it yet). Add `# NOTE: cost_usd is 0.0 until providers implement cost models` comment in `record_usage`
- `usage` CLI command reads `DB_URL` from env (same env var as `run`/`serve`)

### Out of Scope

- Cost model calculation (providers don't emit it yet — tracked separately)
- Scheduler integration (Story 2)
- DynamoDB/Firestore backends (E5)

---

## Story 2: Scheduler

**Type:** Feature  
**Priority:** Should  
**Size:** M (2–3 days)  
**Dependencies:** NONE (independent of Story 1)

### Description

As a bot operator,  
I want to define scheduled tasks in `config.yaml` using cron syntax,  
So that recurring automation runs on schedule without an external queue.

### Technical Context

- **Affected modules:** `monkeybot.core.scheduler` (new), `monkeybot.cli` (wire into `serve`)
- **Design reference:** `1a-discovery.md` "ADR-001, ADR-002", `1b-contracts.md` "core/scheduler.py contracts"
- **Key files to create:**
  - `src/monkeybot/core/scheduler.py` — `JobConfig` dataclass, `Scheduler` class, `_next_run()` helper
  - `tests/unit/test_scheduler.py` — 7 unit tests
- **Key files to modify:**
  - `src/monkeybot/cli.py` — in `_serve_async`: load `scheduler.jobs` from `bot_config`, construct `Scheduler`, call `await scheduler.start()`, wrap in `try/finally` to call `await scheduler.stop()`
  - `bots/example-bot/config.yaml` — add commented-out `scheduler:` section with example job
- **Patterns to follow:**
  - `src/monkeybot/core/history.py` — aiosqlite lazy init pattern for `job_runs` table
  - `src/monkeybot/core/loop.py` — asyncio task lifecycle pattern
- **Dependencies:** NONE (new module; croniter is optional; `asyncio` is stdlib)

### Integration Contracts

**Types defined by this story:**

```python
# src/monkeybot/core/scheduler.py
from dataclasses import dataclass, field
import asyncio

@dataclass
class JobConfig:
    name: str       # unique key, matches job_runs.job_name
    cron: str       # e.g. "0 9 * * *"
    callable: str   # dotted "module.path:function"
    enabled: bool = True

class Scheduler:
    def __init__(
        self,
        db_path: str,
        jobs: list[JobConfig],
        poll_interval: int = 30,
    ) -> None: ...

    async def start(self) -> None:
        """Spawn polling asyncio.Task. Call once per process."""

    async def stop(self) -> None:
        """Cancel polling task and await termination."""
```

**Used by Story 1:** No dependency.  
**Used by cli.py wiring (Phase 6 integration):** `Scheduler` instantiated and started in `_serve_async`.

### `job_runs` table schema

```sql
CREATE TABLE IF NOT EXISTS job_runs (
    job_name  TEXT    PRIMARY KEY,
    last_run  INTEGER,             -- Unix ms, NULL = never run
    next_run  INTEGER NOT NULL     -- Unix ms
);
```

### `config.yaml` scheduler schema

```yaml
scheduler:
  poll_interval: 30          # seconds; default 30
  jobs:
    daily-summary:
      cron: "0 9 * * *"
      callable: "bots.example_bot.jobs:daily_summary"
      enabled: true
```

### Acceptance Criteria

- [ ] **Given** a job with `next_run` in the past (or NULL), **When** `_tick()` is called, **Then** the job callable is invoked and `last_run` + `next_run` are updated in `job_runs`
- [ ] **Given** a job with `next_run` in the future, **When** `_tick()` is called, **Then** the callable is NOT invoked
- [ ] **Given** a job callable that raises an exception, **When** `_tick()` runs, **Then** the error is logged and `next_run` still advances (loop does not crash)
- [ ] **Given** `croniter` is NOT installed, **When** `_next_run()` is called, **Then** returns `now + 1 hour` and logs a WARNING (once per process start)
- [ ] **Given** `croniter` IS installed, **When** `_next_run("0 9 * * *", after)` is called, **Then** returns the correct next 09:00 datetime
- [ ] **Given** `scheduler.start()` called, **When** `scheduler.stop()` is called, **Then** the asyncio task is cancelled and awaited without error
- [ ] **Given** a job with `next_run = NULL` in `job_runs`, **When** first `_tick()` runs, **Then** job fires immediately
- [ ] `scheduler.jobs` absent in `config.yaml` → `Scheduler` is not instantiated (no `job_runs` table created, no task spawned)
- [ ] `ruff check` and `mypy --strict` pass on `core/scheduler.py`

### Scheduler Behaviour Details

- `_tick()` is wrapped in `try/except Exception` — any unhandled error is logged at ERROR level, never re-raised
- Job callable is imported via `importlib.import_module` + `getattr` at startup (not at tick time). `ImportError` → log WARNING, skip job (not fatal)
- Invalid cron string → `ValueError` logged at startup; that job is skipped (not fatal for other jobs)
- `enabled: false` → job loaded but `_tick()` never fires it
- Scheduler runs as `asyncio.Task` alongside uvicorn in `_serve_async`; not started in `monkeybot run` (no scheduler needed for interactive CLI)

### Implementation Notes

- `Scheduler._task: asyncio.Task | None` — set by `start()`, cancelled by `stop()`
- `start()` is a no-op if already started (guard: `if self._task is not None: return`)
- Import `croniter` inside `_next_run()` to keep it truly optional — `ImportError` caught there
- One-time croniter warning: use a module-level `_croniter_warned = False` flag

### Out of Scope

- Starting the scheduler from `monkeybot run` (interactive CLI doesn't need it)
- Usage recording (Story 1)
- Serverless webhook-triggered scheduling (E5)

---

## Phase 6 Integration Points

When both stories are complete, Phase 6 merges these changes into `cli.py`:

1. **`_run_async` and `_serve_async`** — after collecting each `TurnComplete` event, call `await record_usage(db_path, session_id, event)` with proper `try/except`
2. **`_serve_async`** — instantiate `Scheduler` if `scheduler.jobs` in `bot_config`; `start()` before `server.serve()`; `stop()` in `finally`

No other files are shared between stories — integration is two additive hunks to `cli.py`.
