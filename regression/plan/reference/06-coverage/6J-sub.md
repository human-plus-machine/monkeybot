# 6.J Subagents and the task queue (`SUB`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`.

Existing: strong T1 (`test_core_tool_executor.py` task cases, `test_subagent_proto.py`, `test_subagent_worker_paths.py`, `test_worker_pool.py`, `test_progress_publish.py`). T2 `subagent_roundtrip.yaml` is recording-only and would stay green if `_tool_task` returned a stub. T4 `subagents/dispatch_complete`.

| ID | Case | Asserts |
|---|---|---|
| T1-SUB-01 | `child_tool_list` | Worker `build_context` tool names contain no `task`, no loop tools, no `computer_*` (behavioral, not AST). |
| T2-SUB-01 | `subagent_roundtrip` (migrate, real spawn + fake worker) | `SubagentStarted` → `SubagentEvent`s → `SubagentCompleted ok:true`; `progress.jsonl` and `output.json` written. |
| T2-SUB-02 | `nested_task_refused` | Fake child tries `task`: unknown-tool error in child; exactly one `SubagentCompleted`. |
| T2-SUB-03 | `expect_files_present` / `expect_files_absent` | `artifact_exists` true / false; `ok` reflects child exit, not the missing file. |
| T2-SUB-04 | `expect_files_escape` | `expect_files: [../x]`: validation error. |
| T2-SUB-05 | `unknown_subagent_type` | Validation error; no spawn. |
| T2-SUB-06 | `subagent_timeout` | Hanging child, `subagents.timeout_sec: 2`: `ok:false`, error mentions timeout; process group gone. |
| T2-SUB-07 | `parent_cancel` | Cancel during child run: `task: cancelled (parent)`; child killed. |
| T2-SUB-08 | `child_errors_record_failed` | Clean exit with errors in payload: recorded as failed. |
| T2-SUB-09 | `large_ndjson_line` | 1 MiB line parses (limit is 16 MiB, not 64 KiB). |
| T2-SUB-10 | `system_prompt_redacted_on_pipe` | `SystemPromptSnapshot.text` redacted in forwarded events. |
| T2-SUB-11 | `parallel_task_cap` | 12 `task` calls in one batch: at most 10 concurrent. |
| T2-SUB-12 | `persona_applied` | Persona `agent_md` / `model` / `skills` reach the child envelope. |
| T2-SUB-13 | `child_memory_read_only` | Child has palace read, no ingest hook, no writer. |
| T2-SUB-14 | `queue_pending_not_success` | `MONKEYBOT_TASK_QUEUE=1`: pending result with `queued`, `run_id`, `child_thread_id`; no `SubagentStarted`; run row `pending`. |
| T2-SUB-15 | `worker_pool_executes` | Worker pool claims the pending run, writes `progress.jsonl`, records completion. |
| T2-SUB-16 | `worker_stale_reclaim` | Claim older than `MONKEYBOT_WORKER_STALE_CLAIM_MS` (fake clock): reclaimed once. |
| T2-SUB-17 | `ledger_context_forwarded` | Verifier ledger context reaches the `task` envelope (`format_subagent_context`). |
| T3-SUB-01 | `replay_ndjson_tape` | Recorded child NDJSON tape through the parent parser: same `progress.jsonl` bytes and same final event. |
| T4-SUB-01 | `dispatch_complete` (migrate) | As today. |
| T4-SUB-02 | `persona_dispatch` | `required_tools: [task]`; child transcript manifest `subagent_type` matches persona. |
| T5-SUB-01 | trials `subagents` | Reliability on `min_subagent_calls: 1`; zero nested `task` in child transcripts. |
