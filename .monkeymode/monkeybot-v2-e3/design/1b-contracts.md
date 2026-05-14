# Design: monkeybot-v2-e3 — Observability, Scheduling & Cost Tracking
## Phase 1B: Detailed Contracts

**Date:** 2026-05-13  
**Status:** Phase 1B — API Contracts & Integration Points  
**Version:** 1.0

---

## Public Python API Contracts

E3 has no HTTP endpoints — it's a library + CLI feature. The "API" is the public function/class surface.

---

### `core/usage.py`

#### `record_usage(db_path: str, session_id: str, event: TurnComplete) -> None`

Inserts one row into `turn_usage`. Idempotent on `run_id` (UNIQUE constraint — duplicate `run_id` is silently ignored via `INSERT OR IGNORE`).

```python
async def record_usage(
    db_path: str,
    session_id: str,
    event: TurnComplete,
) -> None: ...
```

**Behavior:**
- Opens a fresh `aiosqlite` connection (same pattern as `ConversationHistory`)
- Runs `CREATE TABLE IF NOT EXISTS turn_usage (...)` on first call (lazy init)
- `INSERT OR IGNORE INTO turn_usage ...` using `event.run_id` as the unique guard
- `id` column is a new ULID (independent of `run_id`)
- Returns `None`; on DB error logs via `logging.getLogger("monkeybot.usage")` and re-raises

**Error handling:**
- `aiosqlite.OperationalError` (locked DB) → logged + re-raised; caller (gateway) catches and continues the event stream without crashing the loop

---

#### `get_usage_summary(db_path: str, since_hours: float) -> UsageSummary`

```python
@dataclass
class UsageSummary:
    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_cost_usd: float
    avg_latency_ms: float
    since_hours: float

async def get_usage_summary(
    db_path: str,
    since_hours: float,
) -> UsageSummary: ...
```

**Behavior:**
- `since_epoch_ms = int((time.time() - since_hours * 3600) * 1000)`
- `SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cached_tokens), SUM(cost_usd), AVG(duration_ms) FROM turn_usage WHERE created_at >= ?`
- Returns `UsageSummary` with all zeros if no rows (never raises on empty result)
- Creates the table if it doesn't exist (same lazy init as `record_usage`)

---

### `core/scheduler.py`

#### `Scheduler`

```python
class Scheduler:
    def __init__(
        self,
        db_path: str,
        jobs: list[JobConfig],
        poll_interval: int = 30,
    ) -> None: ...

    async def start(self) -> None:
        """Spawn the polling asyncio.Task. Safe to call once per process."""

    async def stop(self) -> None:
        """Cancel the polling task and await its termination."""

    async def _tick(self) -> None:
        """Check all jobs; fire any with next_run in the past."""
```

##### `JobConfig` (dataclass)

```python
@dataclass
class JobConfig:
    name: str          # unique key, matches job_runs.job_name
    cron: str          # cron expression, e.g. "0 9 * * *"
    callable: str      # dotted path "module.submodule:function"
    enabled: bool = True
```

##### Scheduler behaviour contract

| Scenario | Expected behaviour |
|---|---|
| `next_run` is NULL (never run) | Treated as past — fires on first `_tick` |
| `next_run` is in the past | Fires immediately on next `_tick` |
| `next_run` is in the future | Skipped |
| Job callable raises | Error logged to stderr; `next_run` still advances; loop continues |
| `croniter` not installed | One-time `WARNING` log; `next_run = now + timedelta(hours=1)` |
| `Scheduler.stop()` called | `_task.cancel()`; `asyncio.CancelledError` swallowed cleanly |

##### `_next_run(cron: str, after: datetime) -> datetime`

```python
def _next_run(cron: str, after: datetime) -> datetime:
    """Return the next fire time for *cron* after *after*.
    Falls back to after + 1 hour if croniter is unavailable."""
```

---

### `config.yaml` scheduler schema

```yaml
scheduler:
  poll_interval: 30          # seconds, default 30
  jobs:
    daily-summary:
      cron: "0 9 * * *"
      callable: "bots.example_bot.jobs:daily_summary"
      enabled: true
```

**Validation rules:**
- `scheduler.jobs` is optional; if absent, Scheduler is not started
- `cron` is required per job; invalid cron string → `ValueError` at startup (fail-fast)
- `callable` must resolve via `importlib.import_module` + `getattr`; if not resolvable → `ImportError` logged, job skipped (not fatal)
- `enabled: false` → job loaded but never fired

---

### `cli.py` — `monkeybot usage` command

```
Usage: monkeybot usage [OPTIONS]

  Show token usage and cost summary.

Options:
  --since FLOAT   Look back N hours (default: 24)
  --help          Show this message and exit.
```

**Output format (stdout, plain text):**

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

**Error cases:**
- No `DB_URL` env var set → uses default `sqlite:///data/monkeybot.db`; if file doesn't exist → prints `No usage data found.` and exits 0
- `turn_usage` table doesn't exist (zero turns ever) → prints `No usage data found.` and exits 0

---

## Integration Points

### Events Consumed

| Event | Source | Trigger | Action |
|---|---|---|---|
| `TurnComplete` | `AgentLoop.run()` final `yield` | Every completed agent turn | Call `record_usage()` from gateway layer |

**Wire-up location:** `cli.py` in `_run_async` — after collecting the `TurnComplete` event from the async generator, call `await record_usage(db_path, session_id, turn_complete_event)`.

```python
# In _run_async / _serve_async event consumer:
async for event in agent_loop.run(user_message, session_id):
    ...  # existing gateway handling
    if isinstance(event, TurnComplete):
        try:
            await record_usage(db_path, session_id, event)
        except Exception:
            log.exception("Failed to record usage")
```

### Events Published

None. E3 is a purely observational / scheduling feature.

### External Dependencies

| Dependency | Usage | Optional | Failure handling |
|---|---|---|---|
| `aiosqlite` | `turn_usage` + `job_runs` tables | No (core dep) | Propagate; gateway logs and continues |
| `croniter` | Cron expression parsing | Yes (`[scheduler]` extra) | Fallback to `+1 hour` with warning |
| Python `importlib` | Load scheduler job callables | No (stdlib) | `ImportError` per-job → skip + log |

### Dependency on E1 Contracts

| E1 Symbol | How E3 uses it |
|---|---|
| `TurnComplete` (events.py) | Read `.run_id`, `.input_tokens`, `.output_tokens`, `.cached_tokens`, `.cost_usd`, `.duration_ms` |
| `ConversationHistory._db_path` pattern | `record_usage` uses the same `DB_URL` → same SQLite file |
| `cli.py` `main` click group | `usage` command added to existing group |
| `cli.py` `_serve_async` | Scheduler started here if `scheduler.jobs` in config |

---

## Testing Strategy

### Unit Testing

**Coverage target:** 100% of `core/usage.py` and `core/scheduler.py` critical paths.

**`tests/unit/test_usage.py`**

| Test | Description | Mock boundary |
|---|---|---|
| `test_record_usage_inserts_row` | `record_usage()` writes correct values to temp DB | Real SQLite `:memory:` |
| `test_record_usage_idempotent` | Calling twice with same `run_id` results in 1 row | Real SQLite |
| `test_get_usage_summary_empty` | Returns all-zeros `UsageSummary` when no rows | Real SQLite |
| `test_get_usage_summary_aggregates` | Returns correct sums/avg for 3 inserted rows | Real SQLite |
| `test_get_usage_summary_since_filter` | Rows before `since_hours` cutoff excluded | Real SQLite |

**`tests/unit/test_scheduler.py`**

| Test | Description | Mock boundary |
|---|---|---|
| `test_tick_fires_overdue_job` | Job with `next_run` in past fires on `_tick` | Mock time, mock callable, real SQLite |
| `test_tick_skips_future_job` | Job with `next_run` in future not called | Mock time |
| `test_tick_updates_next_run` | `next_run` advances after job fires | Mock time + croniter |
| `test_tick_job_failure_continues` | Callable raises → `next_run` still advances, no crash | Mock callable |
| `test_croniter_fallback` | Without croniter, `_next_run` returns `+1 hour` | Patch `croniter` import to raise `ImportError` |
| `test_scheduler_start_stop` | `start()` creates task; `stop()` cancels it cleanly | `asyncio` event loop |
| `test_null_next_run_fires_immediately` | Job with NULL `next_run` fires on first tick | Real SQLite |

### Integration Testing

**`tests/integration/test_e3_coldstart.py`** (new)

| Scenario | Components | Expected outcome |
|---|---|---|
| Run a full turn + check usage | `AgentLoop` → `TurnComplete` → `record_usage` → `get_usage_summary` | `summary.turns == 1`, tokens match provider stub |
| `monkeybot usage --since 24` via CLI runner | `cli.py` `usage` command | Non-zero tokens printed |
| Scheduler fires job in test loop | `Scheduler._tick()` with a mock job callable | Callable invoked once; `last_run` updated |

### E2E / Acceptance

| Acceptance criterion | Verification method |
|---|---|
| `monkeybot usage --since 24` shows correct output | `CliRunner` + seeded `turn_usage` rows |
| `record_usage()` inserts row for every `TurnComplete` | Integration test asserting `COUNT(*) = N` after N turns |
| Scheduler fires job with `next_run` in past within 30s | Unit test with `asyncio.sleep` patched + `_tick` called directly |
| Scheduler failure doesn't crash loop | Unit test: callable raises, assert loop still alive |
| `croniter` fallback logs warning | Capture log output; assert `WARNING` present |

---

## Next Steps

- Phase 1C: Shutdown safety (`Scheduler.stop()` on `SIGTERM`), SQLite WAL config for `turn_usage`, cost model accuracy, ruff/mypy compliance notes, monitoring/alerting hooks.
