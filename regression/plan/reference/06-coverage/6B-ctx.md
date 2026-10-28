# 6.B Context, prompts, compaction, epochs (`CTX`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`.

Existing: unit tests for budgets, epochs, prompts, project context, shapers, background summary; loop compaction tests in `test_loop.py`. `memory_wake_up_large.yaml` in T2. `context/summarization_trigger` live scenario is excluded from smoke (passes vacuously or fails depending on model verbosity).

Gaps: `compaction_fallback` untested; epochs never asserted through `run`; nothing pins the stable prefix byte-for-byte across inner turns (prompt-cache busting); no golden for the harness prompt.

| ID | Case | Asserts |
|---|---|---|
| T1-CTX-01 | `harness_prompt_golden` | `compose_stable_baseline` for 6 fixed `TurnContext`s (memory on/off, sandbox on/off, task tool on/off, MCP catalog, emission styles) equals committed goldens. Any prompt edit shows up as a reviewed golden diff. |
| T1-CTX-02 | `volatile_tail_parts_golden` | `compose_volatile_tail_parts` output for fixed date/memory/skills/todos. |
| T2-CTX-01 | `compact_at_trigger` | `config: model.context_window: 4000` + long tool results: `ContextSummarizing` → `ContextSummarized`; one summary row persisted. |
| T2-CTX-02 | `compact_pins_verdict_attachment_thinking` | Verdict row, attachment descriptor, tail thinking survive compaction. |
| T2-CTX-03 | `compact_does_not_persist_repairs` | Repaired tool pairs are not written back. |
| T2-CTX-04 | `compact_keep_tail_ratio` | Tail keeps `max(2, 20%)` rows and never cuts a tool pair. |
| T2-CTX-05 | `compact_fallback` | Summarizer `raise` + > 5000 preloaded rows: `compaction_fallback`; pins kept; `ContextEpochStarted`. |
| T2-CTX-06 | `compact_trailer_from_goal_ledger` | Summary row carries the workspace trailer. |
| T2-CTX-07 | `summarizer_model_override` | `model.summarization_model` is the model of the summary call; summary call has no `vertex_google_search`. |
| T2-CTX-08 | `background_summary_applied` | At the moderate bar a background summary is applied on the next turn when the prefix matches. |
| T2-CTX-09 | `background_summary_stale_fallback` | After a rewind, the stale background summary is discarded and inline compaction runs. |
| T2-CTX-10 | `epoch_volatile_update` | Memory index changes between inner turns: `SystemContextUpdated` with `changed_sources: [memory]`; stable prefix bytes identical. |
| T2-CTX-11 | `epoch_stable_change` | `AGENT.md` / `AGENTS.md` change between turns: `ContextEpochStarted`. |
| T2-CTX-12 | `epoch_after_compaction` | Compaction opens a new epoch. |
| T2-CTX-13 | `stable_prefix_byte_stable` | Across 4 inner turns with tool calls, request 1..4 share an identical stable system prefix. |
| T2-CTX-14 | `project_instructions_spoof_heading` | `AGENTS.md` containing `## Runtime notes` does not land in the volatile tail. |
| T2-CTX-15 | `slash_skill_turn_scoped` | `/diagnostics` adds an invoked-skill section on that turn only. |
| T2-CTX-16 | `tool_result_shaping_under_pressure` | Moderate pressure: tool result trimmed head 70 / tail 30; spill-inventory note kept. |
| T2-CTX-17 | `base64_sanitized` | Tool result with a base64 blob is redacted before the provider (`MONKEYBOT_TOOL_RESULT_SANITIZE` default). |
| T2-CTX-18 | `context_usage_current_not_peak` | `ContextUsage.estimated_tokens` drops after compaction. |
| T2-CTX-19 | `memory_wake_up_large` (migrate) | Large wake-up index injected; turn completes. |
| T3-CTX-01 | `replay_live_compaction` | Recorded long-session cassette compacts at the same call index. |
| T4-CTX-01 | `summarization_trigger` (migrate, retuned) | `config: model.context_window` small enough that the scenario must compact; `min_summarizations: 1`. Fails if it would have passed without compacting. |
| T4-CTX-02 | `post_compaction_continuity` | Multi-step task spanning a forced compaction still completes (workspace grader). |
| T5-CTX-01 | trials of T4-CTX-02 | Capability. |
