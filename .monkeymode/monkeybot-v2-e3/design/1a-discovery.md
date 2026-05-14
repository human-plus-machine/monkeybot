# Design: monkeybot-v2-e3 — Observability, Scheduling & Cost Tracking
## Phase 1A: Discovery & Core Design

---

## Executive Summary

E3 adds two orthogonal capabilities to the monkeybot-v2 harness built in E1/E2: (1) per-turn cost and token usage recording plus a `monkeybot usage` CLI command, and (2) a lightweight cron scheduler that fires recurring jobs defined in `config.yaml`. Both features bolt onto existing infrastructure — the same SQLite DB and `TurnComplete` event already present in `loop.py` — requiring zero new runtime dependencies for usage and one optional dep (`croniter`) for scheduling.

---

## Use Case & Business Value

**US-06 — Cost Observability:** A bot operator runs `monkeybot usage --since 24` to see token counts and USD cost without setting up Datadog, Grafana, or any external tool. The data is already flowing through `TurnComplete`; we just need to capture it and expose it.

**US-10 — Scheduling:** A bot operator defines jobs in `config.yaml` (e.g. a daily summary post, a weekly digest) using familiar cron syntax. The monkeybot process itself fires those jobs on schedule — no Celery, no Redis, no cron daemon needed.

Both stories are "Should" priority and close out Milestone 2.

---

## Technical Context (from codebase inspection)

| Aspect | Current State |
|---|---|
| Language / runtime | Python 3.11+, `asyncio`-native |
| DB | SQLite via `aiosqlite`; WAL mode; single file at `DB_URL` (`sqlite:///data/monkeybot.db`) |
| Existing tables | `messages` (session history) |
| Token data available | `TurnComplete` carries `run_id`, `input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`, `duration_ms` |
| CLI framework | `click` group in `src/monkeybot/cli.py` with `run` and `serve` commands |
| Optional deps | `[scheduler]` optional extra already wired in `pyproject.toml` → `croniter>=2.0` |
| Async execution model | Everything runs inside one `asyncio` event loop (uvicorn / manual `asyncio.run`) |

**Key insight:** `TurnComplete.cost_usd` exists on the event but `loop.py` never writes it anywhere — it just logs to stderr. E3 closes that gap by inserting a `turn_usage` row at the moment `TurnComplete` is yielded.

---

## Architecture Decision

### Chosen Approach

**Thin functions + one Scheduler class, both sharing the existing SQLite DB.**

- `core/usage.py` — two async functions: `record_usage()` (INSERT) and `get_usage_summary()` (SELECT). No class needed; no lifecycle to manage.
- `core/scheduler.py` — `Scheduler` class that owns an `asyncio.Task` polling loop. A class is appropriate: it manages the task handle, the poll interval, and the `next_run` state persisted in SQLite.
- `loop.py` receives a `record_usage` callback at construction time (injected via constructor arg, defaulting to `None` to keep existing call sites green). In the `finally` block, after emitting `TurnComplete`, it calls `await record_usage(...)` if the callback is set.
- `cli.py` `serve` command starts the Scheduler as an `asyncio.Task` if `scheduler.jobs` is present in `config.yaml`.

### Alternatives Considered

| Option | Pros | Cons | Why Not Chosen |
|---|---|---|---|
| Separate `UsageStore` class | Consistent OO style with `ConversationHistory` | Overkill for two functions; no lifecycle to manage | Two standalone async functions are simpler and match the task |
| Scheduler as a separate thread | Avoids asyncio complexity | Shared-state threading bugs; can't `await` tools natively | asyncio.Task is the natural fit for an async-first codebase |
| Inject `db_path` into usage functions | Pure functions, easy to test | Caller must know DB path; harder to mock | Pass `db_path: str` as a parameter — same pattern as `ConversationHistory` |
| Write `turn_usage` directly in `loop.py` | One less file | Mixes persistence concern into the loop; harder to test in isolation | Keep the loop focused on orchestration |

### ADR-001: Scheduler runs as `asyncio.Task`, not thread

**Status:** Accepted  
**Context:** The serve command already owns an `asyncio` event loop (uvicorn). The scheduler needs to fire jobs periodically without blocking the gateway.  
**Decision:** `Scheduler.start()` spawns an `asyncio.Task` that loops with `asyncio.sleep(poll_interval)`.  
**Consequences:**
- Positive: No thread-safety concerns; can `await` coroutine-based job callables natively.
- Negative: Long-running jobs that block the event loop would degrade webhook latency — mitigated by requiring job callables to be non-blocking (documented constraint).
- Risk: If the scheduler task raises an unhandled exception it silently dies — mitigated by wrapping each `_tick()` call in `try/except` and logging.

### ADR-002: `croniter` is optional — graceful fallback to `+1 hour`

**Status:** Accepted  
**Context:** PRT open item: "croniter optional with fallback or silently optional with no pyproject entry?"  
**Decision:** `croniter` stays in the `[scheduler]` optional extra (already there). If not installed, `Scheduler._next_run()` falls back to `now + timedelta(hours=1)` and logs a warning once. This means basic scheduling still works out of the box; operators who need real cron syntax install `monkeybot[scheduler]`.  
**Consequences:**
- Positive: Zero-dep installs work.
- Negative: Non-cron fallback silently changes behavior — mitigated by the one-time warning.

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────┐
│  monkeybot serve / monkeybot run                     │
│                                                      │
│  ┌──────────────┐    TurnComplete    ┌────────────┐  │
│  │  AgentLoop   │ ─────────────────► │ record_    │  │
│  │  (loop.py)   │   (run_id, tokens, │ usage()    │  │
│  └──────────────┘    cost, latency)  │ (usage.py) │  │
│                                      └─────┬──────┘  │
│  ┌──────────────┐                          │         │
│  │  Scheduler   │  asyncio.Task            │ INSERT  │
│  │ (scheduler.py│  _tick() every 30s       ▼         │
│  │   .start())  │               ┌─────────────────┐  │
│  └──────────────┘               │   SQLite DB     │  │
│         │ fires job fn          │  monkeybot.db   │  │
│         ▼                       │                 │  │
│  job callable (async fn)        │  ┌───────────┐  │  │
│                                 │  │  messages │  │  │
│  ┌──────────────┐               │  ├───────────┤  │  │
│  │ monkeybot    │  SELECT from  │  │turn_usage │  │  │
│  │ usage --since│ ◄─────────── │  ├───────────┤  │  │
│  │ (cli.py)     │  turn_usage   │  │ job_runs  │  │  │
│  └──────────────┘               │  └───────────┘  │  │
└─────────────────────────────────┴─────────────────┘
```

---

## Core Data Model

### `turn_usage` table

Stores one row per completed agent turn. Written by `record_usage()`, read by `get_usage_summary()`.

```
turn_usage
├── id            TEXT  PRIMARY KEY  — ULID, consistent with `messages` table
├── run_id        TEXT  NOT NULL     — mirrors TurnComplete.run_id for join-ability
├── session_id    TEXT  NOT NULL     — propagated from AgentLoop.run() call
├── input_tokens  INTEGER NOT NULL DEFAULT 0
├── output_tokens INTEGER NOT NULL DEFAULT 0
├── cached_tokens INTEGER NOT NULL DEFAULT 0
├── cost_usd      REAL    NOT NULL DEFAULT 0.0
├── duration_ms   INTEGER NOT NULL DEFAULT 0
└── created_at    INTEGER NOT NULL  — Unix ms, consistent with messages.created_at

Indexes:
  PRIMARY KEY (id)
  INDEX idx_turn_usage_session ON turn_usage(session_id, created_at)
  INDEX idx_turn_usage_created ON turn_usage(created_at)   -- for --since queries
```

### `job_runs` table (Scheduler state)

Persists `last_run` and `next_run` for each named job so they survive process restarts.

```
job_runs
├── job_name   TEXT    PRIMARY KEY  — matches the key under scheduler.jobs in config.yaml
├── last_run   INTEGER              — Unix ms of last successful fire (NULL = never run)
└── next_run   INTEGER NOT NULL     — Unix ms of next scheduled fire

No additional indexes needed (single-row lookups by PK only).
```

---

## Key Design Decisions

1. **`record_usage()` is called by the gateway layer, not inside `loop.py`.** The loop yields `TurnComplete`; the caller (gateway/CLI) is responsible for persisting it. This keeps the loop's concern as event production, not persistence. In practice, `_run_async` in `cli.py` and `_serve_async` consume the event stream and call `record_usage()`.

   > **Alternative rejected:** Injecting a callback into `AgentLoop.__init__`. Keeps the loop simpler, but the gateway is a cleaner seam.

2. **`get_usage_summary()` returns a typed dataclass** (`UsageSummary`) rather than a raw dict, so the CLI and future callers have IDE support and mypy coverage.

3. **Scheduler jobs are callable names, not shell commands.** The `config.yaml` job entry specifies a Python dotted path (`module:function`) that the Scheduler imports and calls as a coroutine. This keeps jobs first-class Python and avoids shell injection risk.

   > **Fallback:** If the callable raises `ImportError`, the Scheduler logs an error and skips the job — never crashes the main loop.

---

## Next Steps

- **Phase 1B:** Define `record_usage()` / `get_usage_summary()` signatures, `Scheduler` public API, `config.yaml` schema for `scheduler.jobs`, CLI `usage` command contract, and test strategy.
- **Phase 1C:** Error handling for DB unavailability, scheduler task cancellation on shutdown, `SIGTERM` graceful drain, cost model correctness, and ruff/mypy compliance notes.
