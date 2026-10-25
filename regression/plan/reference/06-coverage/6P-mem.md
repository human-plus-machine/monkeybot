# 6.P Memory (`MEM`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools.

Existing: T1 `tests/core/memory/*`, `test_migrate_embedding.py`, `test_memory_layout_migrate.py`. T2 `memory_write_inject`, `memory_wake_up_large`, `tool_search_memory` inject a fake index; none opens the outbox or a palace. T4 `memory/recall_single_session`, `recall_cross_session` (judge + text).

| ID | Case | Asserts |
|---|---|---|
| T1-MEM-01 | `share_threads_flag` | `MONKEYBOT_MEMORY_SHARE_THREADS` off: thread B recall excludes thread A; on: includes. |
| T1-MEM-02 | `drain_timeout_keeps_pending` | Hung embedder: rows stay `pending` after `drain_writer`. |
| T1-MEM-03 | `object_store_uri_fallback` | `gcs://` / `s3://` memory URI falls back to local; `palace_path_from_uri` raises for object stores. |
| T2-MEM-01 | `outbox_ingest_then_inject` | Turn 1 persists; `drain_writer`; turn 2 `PRE_TURN` injects the turn-1 text (content check). |
| T2-MEM-02 | `outbox_thread_isolation` | Thread B never sees thread A's lines with share-threads off. |
| T2-MEM-03 | `ambiguous_commit_no_duplicate` | Simulated `AmbiguousCommitError`: retry once, one history row, one outbox row. |
| T2-MEM-04 | `outbox_failure_does_not_fail_turn` | Outbox write fails after history commit: turn completes; history-only. |
| T2-MEM-05 | `memory_off` | `memory.enabled: false`: no wake-up block, `mempalace search` refused. |
| T2-MEM-06 | `mempalace_search_real` (migrated from `tool_search_memory` in P0B-05 as today; P1-15 extends it to these asserts) | Seeded `InMemoryPalace` behind the search route: stdout contains the drawer; other thread's drawer absent. |
| T2-MEM-07 | `subagent_no_ingest` | Child turn creates no outbox rows. |
| T2-MEM-08 | `memory_write_inject` (migrate) | As today, with content checks. |
| T2-MEM-09 | `ephemeral_palace_shared_outbox_rejected` | Postgres/Firestore shared outbox + ephemeral palace: `MemoryConfigurationError` at boot. |
| T3-MEM-01 | `replay_wake_up` | Recorded `load_index()` lines for a palace snapshot match byte for byte. |
| T4-MEM-01..02 | `memory/recall_*` (migrate) | Plus a sidecar check that the drawer was filed under session 1's thread. |
| T4-MEM-03 | `search_past_session` | Session 2 calls `mempalace search` and answers with the session-1 fact. |
| T5-MEM-01 | trials `memory` | Capability; tool-call rate for `mempalace` reported. |
