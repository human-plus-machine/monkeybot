# Design: monkeybot-v2-e3 — Observability, Scheduling & Cost Tracking
## Phase 1C: Production Readiness

**Date:** 2026-05-13  
**Status:** Phase 1C — Security, Performance, Deployment, Observability, Risk

---

## Security Design

### Input Validation

| Input | Validation rule |
|---|---|
| `--since` CLI arg | Must be a positive float; `click.option(type=float)` enforces type; negative values → `UsageError` |
| `config.yaml` cron string | Validated at startup via `croniter.is_valid(cron)`; invalid → `ValueError` with job name in message |
| `config.yaml` callable string | Must match `module.path:fn` pattern (regex); invalid format → `ValueError` at startup |
| `session_id` passed to `record_usage` | Passed through verbatim; DB parameterized queries prevent injection |

All DB interactions use parameterized queries (`aiosqlite` `?` placeholders) — no string interpolation.

### Data Protection

- `turn_usage` stores tokens + cost + latency. No PII, no message content.
- `job_runs` stores job names and timestamps only. No secrets.
- `DB_URL` (SQLite file path) read from env var only — never hardcoded.
- Scheduler `callable` import path is a config value, not user-supplied at runtime → low injection risk. Validated at startup, not at job execution time.

### Secrets Management

- No new secrets introduced by E3.
- Scheduler job callables may need API keys — that's the job's responsibility to read from env vars, not the Scheduler's.

---

## Performance & Scalability

### Expected Load

E3 is a single-process, single-user bot framework. Not a multi-tenant SaaS service. Scale targets are modest by design.

| Metric | Expected | Design ceiling |
|---|---|---|
| Turns per day | ~100 | 10,000 (SQLite handles easily) |
| Scheduler jobs | 1–10 | 50 (polling is O(n) over jobs table) |
| `turn_usage` rows after 1 year | ~36,500 | SQLite comfortable to ~1M rows |
| `usage` query time | < 5ms | Indexed `created_at` column |

### Performance Targets

| Metric | Target |
|---|---|
| `record_usage()` INSERT latency | < 20ms (WAL mode) |
| `get_usage_summary()` SELECT latency | < 10ms (indexed scan) |
| Scheduler `_tick()` overhead | < 5ms (single SELECT + conditional INSERT) |
| `monkeybot usage --since 24` wall time | < 200ms end-to-end |

### SQLite Tuning

`turn_usage` and `job_runs` share the same DB file as `messages`. The `init()` call in `record_usage` sets WAL mode and `PRAGMA synchronous=NORMAL` on first connection — consistent with `ConversationHistory.init()`.

No connection pooling needed: each aiosqlite context manager is a short-lived exclusive connection, which is correct for SQLite WAL.

### Scheduler Performance

The polling `_tick()` runs every 30 seconds. Each tick:
1. `SELECT * FROM job_runs` → O(jobs) rows
2. For each overdue job: fire callable + `UPDATE job_runs` → O(1) per job

Total `_tick()` DB time for 10 jobs: < 2ms. Negligible.

---

## Deployment Strategy

### Rollout

E3 ships as part of the monkeybot package — no separate deployment. The new code paths activate automatically once `turn_usage` is wired in `cli.py` (always-on) and Scheduler starts only if `scheduler.jobs` exists in config.

### Backward Compatibility

- `monkeybot run` and `monkeybot serve` remain unchanged for users without `scheduler.jobs` in config.
- `turn_usage` table is created lazily on first `record_usage()` call — no migration required.
- `job_runs` table is created lazily in `Scheduler.start()`.
- Zero breaking changes to E1/E2 interfaces.

### Startup Sequence (serve command with scheduler)

```
1. Load config.yaml
2. Validate scheduler.jobs entries (cron strings, callable paths)
   - Any invalid entry → log ERROR + skip that job (don't crash)
3. await history.init()       → ensures messages table
4. await scheduler.start()    → creates job_runs table + spawns asyncio.Task
5. await uvicorn.serve()      → gateway goes live
```

### Shutdown Sequence (SIGTERM)

```
1. uvicorn catches SIGTERM → stops accepting new requests
2. Drains in-flight requests (uvicorn graceful shutdown)
3. cli.py must call await scheduler.stop() before process exit
   → cancels the _tick asyncio.Task
   → any in-flight job callable is given CancelledError
   → job callables should be short-lived; long jobs should handle CancelledError
```

**Implementation note:** `_serve_async` wraps `await server.serve()` in a `try/finally` block that calls `await scheduler.stop()`. The `asyncio.Task` cancellation is awaited to ensure clean exit.

### Health Check Integration

No new health check endpoints (E3 is not a service). The `Scheduler` task liveness is monitored via its `asyncio.Task` reference — if the task has an exception, it's logged in `_tick`'s outer `try/except`.

---

## Observability

### Logging

All logging via standard `logging` module with logger name `monkeybot.usage` and `monkeybot.scheduler`. Uses the existing JSON formatter from `cli.py`.

**`monkeybot.usage` log events:**

| Level | Message | When |
|---|---|---|
| DEBUG | `"usage recorded run_id=%s cost_usd=%.6f"` | Every successful `record_usage()` |
| ERROR | `"Failed to record usage run_id=%s"` + exc_info | DB write fails |

**`monkeybot.scheduler` log events:**

| Level | Message | When |
|---|---|---|
| INFO | `"scheduler started poll_interval=%ds jobs=%d"` | `start()` called |
| INFO | `"job fired name=%s"` | Job callable invoked |
| WARNING | `"croniter not available — using +1 hour fallback"` | One-time on import |
| WARNING | `"job callable not importable name=%s callable=%s"` | `ImportError` at startup |
| ERROR | `"job failed name=%s"` + exc_info | Job callable raises |
| INFO | `"scheduler stopped"` | `stop()` called |

### Metrics (structured log fields)

No Prometheus/OpenTelemetry in E3 (out of scope for single-process bot framework). The `monkeybot usage` CLI and SQLite `turn_usage` table **are** the observability layer.

Future extension point: `record_usage()` could emit structured log fields that operators pipe to a log aggregator. The current `turn_complete` log entry in `loop.py` already does this — E3 complements it with DB persistence.

### `monkeybot usage` output

The CLI command surfaces the data for human operators. Format specified in Phase 1B contracts — plain text, no external dependencies.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| SQLite locked during concurrent writes | Low | Low | WAL mode; `record_usage` is fire-and-forget from the gateway; if it fails, it logs and continues — no turn is blocked |
| `turn_usage` table missing (first run before E3 deployed) | Low | Low | Lazy `CREATE TABLE IF NOT EXISTS` on first call |
| Scheduler job callable blocks the event loop | Medium | Medium | Document that job callables must be short-lived async functions; long operations must use `asyncio.to_thread` internally. Scheduler does not enforce this — it's an operator contract. |
| `croniter` not installed, operator expects real cron | Medium | Low | One-time WARNING log clearly states the fallback. Docs updated in `README`. |
| Scheduler task silently dies after exception | Low | Medium | `_tick` is wrapped in `try/except Exception`; uncaught `BaseException` (e.g. `MemoryError`) would kill the task — acceptable for a single-process bot. |
| Job runs duplicate on restart (next_run not persisted) | Low | Medium | `job_runs` table persists `next_run` to SQLite — survives restarts. On first run (NULL `next_run`), fires immediately, which is the correct behaviour per spec. |
| Cost USD inaccuracy (`TurnComplete.cost_usd` = 0) | High | Low | E1's `loop.py` currently sets `cost_usd=0.0` (not calculated). E3 records whatever the event carries. The `usage` command will show $0.00 until a provider calculates cost. This is a known limitation; resolving it is E4/future work. Add a comment in `record_usage`. |
| **Incompatible with serverless / ephemeral compute** | High (if deployed on Lambda/AgentCore/Agent Engine) | High | See section below. Tracked as E5. |

### Risk: `cost_usd` always 0

This is the most significant operational risk. `TurnComplete` has a `cost_usd` field, but neither the Gemini nor Claude provider currently populates it — the loop passes the raw `ProviderDone.usage` into the event without a cost model.

**Mitigation for E3:** `get_usage_summary()` will return 0.0 for `total_cost_usd` and the CLI will display `$0.0000`. This is correct — it accurately reflects that cost calculation is not yet wired. A `# TODO: cost model` comment in `loop.py` tracks the gap. Users are not misled.

### Risk: Serverless / Ephemeral Compute Incompatibility

**Scope:** E3's scheduler and the broader monkeybot-v2 persistence layer (including E1's `ConversationHistory`) are fundamentally incompatible with serverless runtimes such as AWS Lambda, AWS AgentCore, and GCP Agent Engine.

**Root causes (two separate issues):**

1. **Scheduler requires a persistent process.** The `asyncio.Task` polling loop only runs while the process is alive. Serverless functions are invoked per-request and frozen/terminated between invocations — the `_tick()` loop never fires between requests. Jobs would silently never run.

2. **SQLite requires a local, persistent filesystem.** All three persistent stores (`messages`, `turn_usage`, `job_runs`) write to a single SQLite file on disk. Lambda's filesystem is ephemeral and not shared across instances. Multi-instance deployments would each have an isolated, blank database — conversation history, usage data, and scheduler state are lost between cold starts.

**E3 deployment constraint:** E3 is designed for **long-running process deployments** only:
- Docker containers (ECS, Cloud Run with `min-instances: 1`, Fly.io, Railway)
- VMs / bare-metal
- Any runtime where `monkeybot serve` stays alive continuously

**Workaround for serverless operators (not implemented in E3):** Use platform-native scheduling to POST to the existing webhook endpoint on a cron schedule (AWS EventBridge Scheduler → `POST /webhook`, GCP Cloud Scheduler → `POST /webhook`). No in-process scheduler needed. The `turn_usage` and `ConversationHistory` problems require E5.

**Resolution:** Tracked as **E5: Serverless Portability**. E5 will introduce a pluggable storage backend abstraction (replacing the SQLite-direct calls in `ConversationHistory`, `record_usage`, and `Scheduler`) with adapters for DynamoDB, Firestore, and Redis, enabling stateless multi-instance deployments. See `.prt/monkeybot-v2/epics/e5-serverless-portability.md`.

---

## Definition of Done Checklist

- [x] Security: parameterized queries, no PII, no new secrets
- [x] Performance: WAL mode, indexed `created_at`, lazy init
- [x] Deployment: backward-compatible, lazy table creation, graceful shutdown
- [x] Observability: structured logs for every scheduler lifecycle event
- [x] Risks: identified and mitigated (or accepted with rationale)
- [x] `ruff check` + `mypy --strict` compliance: all new code uses type hints, no `Any` escapes without comment
