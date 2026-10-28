# 6.L Scheduler, loops, durable goals (`SCHED`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`.

Existing: T1 `test_scheduled_loops.py`, `test_firestore_scheduled_loops.py`, `tests/scheduler/test_engine.py`, `test_goals.py`, `test_goal_persistence.py`, `test_scheduler_routes.py`. T2 `tool_loops.yaml` recording only. No live scenario.

| ID | Case | Asserts |
|---|---|---|
| T1-SCHED-01 | `interval_parse_floor` | `4s` raises; `5s` → 5000; `5m` → 300000; malformed stored intervals skipped by `list_due`. |
| T2-SCHED-01 | `enable_loops_progressive` | Loop tools absent until `enable_loops`; present on the next request; `disable_loops` removes them. |
| T2-SCHED-02 | `loop_lifecycle_real_store` | `start_loop` (confirmed) → `loop_status` → `pause_loop` → `resume_loop` → `stop_loop`: row ends `completed`. |
| T2-SCHED-03 | `start_loop_requires_stop_guard` | No `max_ticks` / `max_runtime` and not `unbounded`: validation error. |
| T2-SCHED-04 | `loop_goal_isolation` | `pause_loop` on a goal id: `unknown loop`. |
| T2-SCHED-05 | `one_tick_fake_clock` | Advance clock past `next_tick_at_ms`: exactly one invoke with `format_tick_prompt`. |
| T2-SCHED-06 | `busy_session_defers` | `skip_if_busy`: busy session defers; `defer_tick` keeps a reclaimed claim. |
| T2-SCHED-07 | `loop_error_fails_goal_retries` | Loop tick error fails the loop; goal tick error stays active until 3 consecutive failures. |
| T2-SCHED-08 | `stale_claim_reclaimed` | Fake clock past `STALE_CLAIM_MS`: reclaimed; heartbeat prevents reclaim during a slow tick. |
| T2-SCHED-09 | `create_goal_requires_slash_goal` | `create_goal` without `/goal`: error; with it: one row. |
| T2-SCHED-10 | `one_open_goal_per_session` | Second `create_goal` with a different objective: "a goal is already active in this session". |
| T2-SCHED-11 | `tool_loops` (migrate, recording) | As today. |
| T2-SCHED-G1 | `invoke_tick_busy_409` (gateway) | `POST /scheduler/invoke-tick` during a turn: 409 `SESSION_BUSY`; loop not failed. |
| T2-SCHED-G2 | `routes_lifecycle` (gateway) | `/scheduler/loops` CRUD and `/goals` lifecycle; reserved session ids rejected; `confirmed=true` required. |
| T4-SCHED-01 | `http_one_tick_live` | Create via HTTP + invoke-tick: history gains one user message equal to the tick prompt; a real reply follows. |
| T4-SCHED-02 | `model_starts_loop` | "Check X every 5 minutes" → `required_tools: [start_loop]` with a stop guard. |
| T5-SCHED-01 | trials of T4-SCHED-02 | Capability. |
