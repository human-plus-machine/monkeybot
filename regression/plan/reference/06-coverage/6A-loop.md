# 6.A Turn loop and harness guards (`LOOP`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`.

Existing: strong loop-level Python tests in `tests/core/test_loop.py`, `test_doom_loop.py`, `test_empty_completion.py`, `test_truncated_tool_batch.py`, `test_background_jobs.py`, `test_input_admission.py`, `test_tool_integrity.py`, `test_provider_stream_mapper.py`. Only `turn_completion.yaml` exists as a scenario, because the YAML schema cannot express thinking, truncation, usage, cancel, steer, or confirm.

Gaps: the guards are invisible to the scenario layer; no T3 replay of a real doom loop or truncation; `FakeProvider` returns nothing past the script.

| ID | Case | Asserts |
|---|---|---|
| T2-LOOP-01 | `turn_completion` (migrate) | Text-only turn completes, one provider call. |
| T2-LOOP-02 | `max_turns_cap` | Tool call every turn with `max_turns: 2`: interventions `[max_turns]`, `Error` text exactly `Max turns exceeded`. |
| T2-LOOP-03 | `max_turns_not_on_clean_finish` | Text finish on the last allowed turn emits no `max_turns`. |
| T2-LOOP-04 | `truncated_batch_rejected` | `done: {truncated: true}` + tool call: `truncated_batch` detail `stream_truncated`; executor never called; error `ToolCallResult` emitted. |
| T2-LOOP-05 | `all_parse_error_batch_rejected` | Every call has `parse_error`: `truncated_batch` detail `all_parse_error`. |
| T2-LOOP-06 | `mixed_parse_error_batch_runs_valid` | One valid + one broken call: valid one executes. |
| T2-LOOP-07 | `doom_identical_read` | Three identical `read_file` calls: `doom_loop`; next provider request has empty tools; recovery note in system text. |
| T2-LOOP-08 | `doom_path_retry_spelling` | Three failing `replace_in_file` on `./a` vs `a` with different hunks: `doom_loop`. |
| T2-LOOP-09 | `doom_path_reset_on_success` | Fail, fail, success, fail on one path: no trip. |
| T2-LOOP-10 | `doom_exempt_poll` | `loop_status` / `await_command` between failing edits neither hides nor trips the streak. |
| T2-LOOP-11 | `doom_disabled` | `DOOM_LOOP_THRESHOLD=0`: no trip after 5 identical calls. |
| T2-LOOP-12 | `empty_then_text` | Silent `done`, then text: `empty_completion` detail `retry`; no `Error`. |
| T2-LOOP-13 | `empty_exhausted` | Three silent `done`s: `empty_completion` detail `exhausted`; exact error string. |
| T2-LOOP-14 | `post_tool_empty_retry` | Tool call, silent `done`, text: `post_tool_empty`; history still holds the tool row. |
| T2-LOOP-15 | `thinking_only_is_empty` | Thinking deltas then `done` with no text: treated as empty, retried. |
| T2-LOOP-16 | `background_nudge_then_release` | Executor reports a job for two checks: `background_jobs_nudge` ×2; held text absent from events; later text released. |
| T2-LOOP-17 | `background_check_raises_fails_closed` | `running_background_jobs()` raises: `Error` `jobs_check_failed`; no clean finish. |
| T2-LOOP-18 | `background_nudges_exhausted` | Job never ends: 3 nudges then kill error; no double reporting. |
| T2-LOOP-19 | `steer_mid_turn` | `steps: steer` after first tool batch: `UserSteered` before provider call 2; steer text in that request's last message. |
| T2-LOOP-20 | `promote_follow_up_provenance` | Promoted follow-up: `UserSteered.queued_request_id` set; not duplicated. |
| T2-LOOP-21 | `cancel_mid_stream` | `steps: cancel` after first text delta: partial assistant row persisted; `Error` `Request cancelled`; no tool executes. |
| T2-LOOP-22 | `cancel_after_tool_call_before_execute` | Synthetic cancel `ToolResponse` in history; invariant 3 holds. |
| T2-LOOP-23 | `confirm_approve` | Confirm inspector + approve: tool executes once. |
| T2-LOOP-24 | `confirm_deny` | Deny: no execute; error tool row; turn continues. |
| T2-LOOP-25 | `confirm_timeout` | No answer: synthesized error; not executed. |
| T2-LOOP-26 | `confirm_always_remembered` | `always: true`: second identical call in the same session skips the confirm. |
| T2-LOOP-27 | `parallel_results_in_submission_order` | Two parallel-safe calls then a mutating call: results in one user row in submission order. |
| T2-LOOP-28 | `parallel_concurrency_cap` | 12 parallel-safe calls with `MONKEYBOT_PARALLEL_TOOL_CONCURRENCY=10`: never more than 10 in flight. |
| T2-LOOP-29 | `registry_refresh_same_turn` | `enable_mcp` success: next provider request includes the new tools. |
| T2-LOOP-30 | `repair_orphan_tool_response` | Preloaded orphan `ToolResponse`: provider sees synthetic request; stored rows unchanged. |
| T2-LOOP-31 | `repair_missing_tool_result` | Preloaded `ToolRequest` without result: provider sees interrupted-result text. |
| T2-LOOP-32 | `ask_user_roundtrip` | Scripted `ask_user`: `AskUserRequest` event; preset answer becomes the tool result; turn completes. |
| T2-LOOP-33 | `ask_user_non_interactive` | No pending bus: tool error, no hang. |
| T2-LOOP-34 | `thinking_block_order` | Thinking deltas then text then tool: block start/delta/complete ordering; thinking closed before tools. |
| T2-LOOP-35 | `thought_signature_preserved` | `ToolCall.metadata.thought_signature` on first call survives into the next request. |
| T2-LOOP-36 | `usage_totals` | Two calls with `usage` events: `TurnComplete.usage` sums; missing usage stays 0. |
| T2-LOOP-37 | `provider_raises` | `raise` mid-stream: `Error` then `TurnComplete`; partial text persisted. |
| T2-LOOP-38 | `tool_input_delta_passthrough` | `tool_input_delta` events are forwarded and do not execute anything before `ToolCall`. |
| T2-LOOP-39 | `grounding_event_passthrough` | `grounding` event yields `GroundingEvent` with sources unchanged. |
| T2-LOOP-40 | `resume_thinking_budget_routine` | `MONKEYBOT_RUN_KIND=routine` resume turn on gemini/claude/ollama sets the resume thinking budget; other providers untouched. |
| T3-LOOP-01 | `replay_live_doom_loop` | A recorded live run that tripped the doom loop still trips it at the same call index. |
| T3-LOOP-02 | `replay_live_truncation` | A recorded `finish=length` tool batch is still rejected. |
