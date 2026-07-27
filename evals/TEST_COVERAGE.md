# Eval coverage — harness components vs. what's actually tested

Working doc, not committed documentation. Extend this as coverage changes; delete once the
gaps below are closed or tracked elsewhere.

Two eval layers exist:
- **Live** (`evals/scenarios/*.yaml`) — real model, real gateway, scored by an LLM judge + harness
  assertions. Slow, costs tokens, subject to provider rate limits.
- **Deterministic** (`tests/evals/scenarios/*.yaml`) — fake/scripted provider via `tests/evals/`,
  runs in pytest, free, sub-second. Good for exact tool-routing/plumbing checks that don't need a
  real model's judgment.

Tool/component list pulled from `src/monkeybot/core/tools/core_tool_executor.py` and
`src/monkeybot/core/{mcp,memory,context,subagents}/`.

## Coverage table

| Harness component / tool | Live eval | Deterministic eval | Status |
|---|---|---|---|
| `read_file` / `load_file` | `tools/core_read.yaml` | `tool_read.yaml` | **Pass** (live + deterministic). Was failing on `max_tool_errors: 1 > 0` for a self-corrected bad-path retry — fixed by changing `EvalRun.tool_errors_count()` ([evals/models.py](models.py)) to only count errors with no later successful call to the same tool. Reverified live against `inkling`: 1/1 pass, 0 tool errors. |
| `read_file` error path | `tools/core_read_error.yaml` (exists) | — | Not in `evals/suites/smoke.yaml` — never runs in CI, not run this session. |
| `write_file` / `replace_in_file` / `apply_patch` | `tools/core_write.yaml` (new) | `tool_write.yaml` (new) | **Closed.** Live scenario writes a scratch file via `write_file`; verified live against `inkling`, 1/1 pass, 0 tool errors. `replace_in_file`/`apply_patch` still have no dedicated scenario — only `write_file` is exercised. Added to `evals/suites/smoke.yaml`. |
| `glob` / `grep` | — (incidental only, via `core_read`) | — | **GAP.** No dedicated scenario or assertion. |
| `search_memory` / `edit_memory` / `update_memory` / `forget` | `memory/recall_single_session.yaml`, `memory/recall_cross_session.yaml` | `tool_search_memory.yaml`, `memory_write_inject.yaml` | Pass. `recall_cross_session` errored in the full 7-scenario run from cumulative NVIDIA rate-limiting (confirmed not a real bug — passes standalone). `edit_memory`/`forget` have no dedicated scenario. |
| `list_skills` | `skills/skill_invocation.yaml` | — | Pass |
| `task` (subagent dispatch) | `subagents/dispatch_complete.yaml` | `subagent_roundtrip.yaml` | Pass |
| `run_command` (shell/terminal) | `tools/core_run_command.yaml` (new) | `tool_run_command.yaml` (new) | **Closed.** Runs `echo` via `run_command` with `SANDBOX_ENABLED=false` (plain `TerminalExecutor`, no Docker) — confirmed this works, `echo` is allowlisted. Verified live against `inkling`, 1/1 pass, 0 tool errors. Added to `evals/suites/smoke.yaml`. |
| MCP: `enable_mcp`/`disable_mcp`/`list_mcp_resources`/`read_mcp_resource`/`list_mcp_prompts`/`get_mcp_prompt` + tool invocation | `mcp/tool_invoke.yaml` (exists) | — | Tagged `local-only`; **not part of CI** (no in-process MCP fixture yet — see file comment + `docs/live-evals.md`). Resource/prompt tools have zero coverage even locally. |
| Loops/scheduler: `start_loop`/`loop_status`/`pause_loop`/`resume_loop`/`stop_loop` | — | — | **GAP.** Zero coverage. |
| `web_search` | — | — | **GAP.** Zero coverage. |
| Todo list tool | — | — | **GAP.** `multi_turn/task_tracking` only tests conversational coherence, not the todo tool. |
| Knowledge index / codebase `search` tool | — | `test_memory_knowledge_routing.py` (routing heuristic only) | Only checks which tool *should* be picked in principle; never actually invokes the tool via a live/scripted turn. |
| Attachments | — | — | **GAP.** Zero coverage. |
| Sandbox executor | — | — | **GAP.** CI always runs `SANDBOX_ENABLED=false`; sandboxed command execution never exercised. |
| Tool-integrity repair (`repair_tool_turn_integrity`) | — | `tests/core/test_tool_integrity.py` (10 unit tests, pre-existing) + `test_run_replays_repaired_history_to_provider` (new, `tests/core/test_loop.py`) | **Corrected — this doc's old claim was stale.** Unit-level repair coverage already existed (missing-result synthesis, orphan handling, empty-tool-name backfill, provider round-trips) and runs under plain pytest. The real gap was narrower: nothing proved repair actually runs *inside* the turn loop before a provider replay. Closed with one integration test that seeds a corrupted `FakeHistory` and asserts the provider sees a repaired transcript while the stored rows stay untouched. |
| Context curation / summarization | `context/summarization_trigger.yaml` | `context_curation.yaml` | Deterministic passes. Live **FAILS** on `inkling`: `min_summarizations: 0 < 1`. Root-caused (not a capture bug): the trigger fires off a local token estimate of the outbound prompt crossing `context_window_tokens * 0.85`, independent of model/output shape. `inkling`'s replies are much terser (~124 output tokens/turn observed) than the model this scenario was tuned against, so the same 5-turn conversation accumulates prompt tokens more slowly and may never cross the 6800-token cap (8000 window) before the scenario ends. Not yet reproduced with instrumented `ContextUsage` numbers — see open question below. |
| Multi-turn coherence | `multi_turn/task_tracking.yaml` | `turn_completion.yaml` | Pass |

## Biggest gaps to close first (suggested priority) — gaps 1-3 closed 2026-07-27

1. ~~`write_file` / `replace_in_file` / `apply_patch`~~ — `write_file` now covered (live + deterministic); `replace_in_file`/`apply_patch` still open.
2. ~~`run_command`~~ — closed, live + deterministic, confirmed working under `SANDBOX_ENABLED=false`.
3. ~~Tool-integrity repair~~ — closed; turned out to be a documentation error (solid unit coverage already existed), plus one new loop-integration test for the actual narrow gap.
4. MCP — get the local-only scenario into CI once a fixture exists; add resource/prompt tool
   coverage.
5. Sandbox executor — nothing exercises `SANDBOX_ENABLED=true` behavior at all.
6. Loops/scheduler, web_search, todo_list, attachments, knowledge `search`, `glob`/`grep`,
   `replace_in_file`/`apply_patch` — lower-traffic surfaces, but currently zero dedicated coverage each.

## Open questions from the current `thinkingmachines/inkling` run

- `tools/core_read`: **resolved** — self-corrected errors no longer count toward `max_tool_errors`; reverified live.
- `context/summarization_trigger`: root-caused as inkling's terser output slowing token accumulation, not a harness bug — still needs a live rerun with gateway `ContextUsage` events inspected to confirm the estimate genuinely falls short of the 6800-token cap, rather than something else suppressing the trigger.
