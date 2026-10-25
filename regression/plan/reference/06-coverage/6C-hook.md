# 6.C Hooks, evidence guard, secret egress (`HOOK`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement.

Existing: `test_hooks.py`, `test_loop_hooks.py`, `test_settlement.py`, `test_evidence_guard.py` (hook-level only), `test_secret_egress.py`.

Gaps: evidence guard never runs through `run`; scenarios cannot register hooks except `gate: true`.

| ID | Case | Asserts |
|---|---|---|
| T2-HOOK-01 | `hook_lifecycle_order` | Recording hook sees `USER_MESSAGE, PRE_TURN, TOOL_DEFINITION, BEFORE_PROVIDER_REQUEST, AFTER_PROVIDER_RESPONSE, PRE_TOOL, POST_TOOL, …, PRE_FINAL_ANSWER, POST_TURN` in that order for a one-tool turn. |
| T2-HOOK-02 | `pre_turn_once` | `PRE_TURN` fires on inner turn 1 only. |
| T2-HOOK-03 | `hook_exception_is_logged` | Raising hook does not abort the turn. |
| T2-HOOK-04 | `hook_timeout_continues` | Hook exceeding its timeout is abandoned; turn continues. |
| T2-HOOK-05 | `settlement_before_turn_complete` | Slow `POST_TOOL` write-side hook completes before `TurnComplete`. |
| T2-HOOK-06 | `tool_definition_replacement` | Hook that replaces tools changes the next request's tool list. |
| T2-HOOK-07 | `history_compacted_awaited` | `HISTORY_COMPACTED` hook runs before the post-compaction provider call. |
| T2-HOOK-08 | `evidence_missing_path` | `hooks: [evidence_guard]`, answer with `Evidence: missing.py`: correction in the next request's system text; only once. |
| T2-HOOK-09 | `evidence_unread_then_read` | Citation of an unread real file: notice once; after `read_file`, no notice. |
| T2-HOOK-10 | `evidence_grep_only_not_accepted` | File only seen via `grep` still counts as unverified. |
| T2-HOOK-11 | `evidence_thread_isolation` | Reads in session A do not satisfy citations in session B. |
| T2-HOOK-12 | `secret_redacted_interactive` | Tool result containing a canary: `CredentialEgressBlocked`; next request redacted; turn continues. |
| T2-HOOK-13 | `secret_ends_routine_turn` | `MONKEYBOT_RUN_KIND=routine`: `Error` and no second provider call. |
| T2-HOOK-14 | `secret_scanner_failure_fails_closed` | Scanner raises: content withheld. |
| T2-HOOK-15 | `secret_in_tool_args` | Secret in `write_file.content`: `credential_egress_blocked`; file not created (also POL-09). |
| T4-HOOK-01 | `evidence_citation_live` | Ask for an answer with `Evidence:` paths; every cited path exists and was read (code grader). |
