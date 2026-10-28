# 6.Q Persistence, backends, migrations (`STORE`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`.

Existing: SQLite well covered in T1. Only `test_branch_store_backends.py` is a real multi-backend contract (Postgres/Firestore skip without env). Many Postgres/Firestore paths are mocks. Goal ledger is SQLite/in-memory only; Postgres and Firestore return `None`. Usage, runs, loops, goal ledger, and turn locks are not `agent_scope`d.

**T1 backend parity matrix** (`tests/contracts/test_storage_parity.py`, parameterized over `open_backend(kind)`):

| ID | Case | Asserts |
|---|---|---|
| T1-STORE-01 | `history_roundtrip_scope_idempotent` | Two scopes; same `message_id` twice → one row; other scope's `list_threads` empty. |
| T1-STORE-02 | `reset_preserves_order_and_row_ids` | |
| T1-STORE-03 | `delete_rows_legacy_and_stored` | `legacy:<key>` and real ids. |
| T1-STORE-04 | `hidden_threads` | `subagent:` and branch threads absent from `list_threads`. |
| T1-STORE-05 | `append_with_outbox_atomic` | Outbox failure rolls back the history row. |
| T1-STORE-06 | `branches_one_active` | Concurrent creates leave one active branch. |
| T1-STORE-07 | `branch_ops_parity` | Edit / rewind / fork / point-survives-compaction on every backend. |
| T1-STORE-08 | `usage_breakdown_buckets` | hour/day/week keys match `utc_bucket_key`. |
| T1-STORE-09 | `run_claim_single_winner_stale_reset` | |
| T1-STORE-10 | `loop_claim_defer_complete` | |
| T1-STORE-11 | `turn_lock_exclusive_and_stale` | |
| T1-STORE-12 | `outbox_claim_scoped` | Other `palace_id` / `agent_id` not claimed; gc respects age. |
| T1-STORE-13 | `goal_ledger_presence` | SQLite: `commit_classified` rolls back on failure. Postgres/Firestore: `None` (pinned until implemented). |
| T1-STORE-14 | `known_unscoped_tables` | Pins today: usage/runs/loops/locks visible across agent scopes on a shared DB. Flip when scoping lands. |

Migrations and the rest:

| ID | Case | Asserts |
|---|---|---|
| T1-STORE-20 | `legacy_db_apply_schema` | Each `fixtures/legacy_db/*.sqlite` through `apply_schema`: columns present, row counts kept, legacy rows load as `legacy:<key>`. |
| T1-STORE-21 | `create_schema_matches_apply_schema` | A fresh database from `scripts/create_schema.py` has the same `sqlite_master` snapshot as `apply_schema`. |
| T1-STORE-22 | `auto_schema_false` | `paths.auto_schema: false` skips DDL; missing `memory_outbox` fails ready. |
| T1-STORE-23 | `embedding_migrate_lock` | Second migrator skips while the lock is held; explicit Gemma palaces untouched. |
| T1-STORE-24 | `layout_migrate_nonempty_dest` | Non-empty destination not merged. |
| T1-STORE-25 | `firestore_summary_doc_migration` (`needs_firestore`) | Old summary doc removed, scoped doc written. |
| T2-STORE-01 | `compaction_reset_row_ids` | After a scripted compaction, `load` has the summary row and preserved tail `row_id`s. |
| T2-STORE-02 | `usage_recorded_on_turn_complete` | One `turn_usage` row per user message matching `TurnComplete.usage`. |
| T2-STORE-03 | `transcript_convertible` | `runtime.transcript_enabled: true` run produces a transcript that `from_transcript.py` converts into a cassette that replays green (closes the T3 loop). |
| T2-STORE-G1 | `rewrite_regenerate_replays_tool_safe` (gateway) | Regenerate across a tool pair: `HistoryRewritten` then replay turn; tool pairs intact. |
