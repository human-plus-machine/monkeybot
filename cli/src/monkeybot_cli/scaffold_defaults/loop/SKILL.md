---
name: loop
description: Run prompt-first scheduled loops with user confirmation, guards, and progressive loop tools.
---

# Loop

Use this skill when the user wants recurring agent work on an interval
(for example "check deploy every 5m" or "poll CI until green").

## Prerequisites

1. Durable storage must be configured (`paths.db_url` / `DB_URL`).
2. A scheduler worker must be running (`MONKEYBOT_SCHEDULER_ENABLED=1` or the
   standalone scheduler process).
3. Call `enable_loops` first if `start_loop` / `loop_status` / `pause_loop` /
   `resume_loop` / `stop_loop` / `disable_loops` are not yet in the active tool
   list. New tools appear on the **next model step this turn**.

## Start a loop

1. Agree the plan with the user (what to do each tick, and when to stop).
2. Put that plan in `prompt` using clear BUSINESS / RULES sections.
3. Choose `interval` (`20s`, `5m`, `1h`, …).
4. Prefer a hard guard: `max_ticks` and/or `max_runtime` (e.g. `1h`).
   Use `unbounded=true` only with explicit user confirmation.
5. Call `start_loop`. The harness always asks the user to confirm before the
   loop is registered.
6. Soft stop criteria ("stop when CI is green") belong in the tick `prompt`;
   on a later tick, call `stop_loop` when they are met.

## Manage a loop

- `loop_status` — one loop or list all (safe to poll; doom-loop exempt).
- `pause_loop` / `resume_loop` — temporary hold.
- `stop_loop` — permanent stop.
- `disable_loops` — hide loop tools from the next model step; does **not** stop
  running loops (call `stop_loop` first).

## Guidance

- Read this skill before inventing your own scheduling approach.
- Do not start duplicate loops for the same purpose; check `loop_status` first.
- Keep tick prompts focused; avoid noisy side effects every tick.
