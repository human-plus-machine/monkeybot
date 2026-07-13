# Ticket: Eval PR Scorecard

## Why this matters

Right now, when a harness PR changes prompts, tool wiring, MCP integration, memory,
context summarization, or model config, nobody can answer "did this make monkeybot better
or worse, and did it break anything?" without eyeballing a demo. The eval service already
runs scenarios and scores them, but nothing persists a run, nothing compares two runs, no
one has wired it into CI, and — most importantly — the 3 scenarios that exist today only
check "does the reply sound plausible" (LLM judge), not "did the right subsystem actually
fire." A PR that silently breaks skill invocation, MCP tool calls, or subagent dispatch
would still pass every scenario in the suite today, because nothing asserts on the
underlying tool/span data.

The goal: a PR gets a pasteable report that says PASS/FAIL against the last agreed-good
baseline, across quality, cost, latency, and — critically — whether each major harness
subsystem still does what it's supposed to.

## Relationship to `tests/evals/` (T2 loop evals)

monkeybot already has a **second eval system** that the live eval service plan must not
duplicate blindly.

| | `tests/evals/` (T2) | `evals/` (live E2E) |
|---|---|---|
| **What it drives** | `monkeybot.core.runtime.loop.run` with a scripted fake provider | Live gateway over HTTP + SSE |
| **Assertions today** | Mechanistic: `tool_categories_used`, `memory_injected`, `turn_completed`, `no_errors` ([scenario_runner.py](../tests/evals/scenario_runner.py)) | Judge-only: deepeval metrics on prose |
| **CI today** | Yes — runs via `uv run pytest tests/` on every push/PR | No |
| **What it catches** | Loop/hook/tool wiring regressions with deterministic replay | Real provider quirks, gateway/SSE integration, MCP auth, model behavior |
| **What it misses** | Real LLM decisions, MCP servers, provider SDK shape changes | Nothing, if scenarios + telemetry are complete |

**Positioning:** live evals are the **E2E complement** to T2 loop evals, not a replacement.
T2 stays fast and deterministic in ordinary CI; live evals gate release promotion and
dependency bumps where real-provider behavior matters.

**Reuse, don't reinvent:** port the assertion vocabulary from `tests/evals/scenario_runner.py`
(`tool_categories_used` → live `required_tools` / `tool_calls_by_name`, `memory_injected` →
live memory telemetry, etc.) into the live scenario schema. The two systems can share naming
even though T2 uses scripted turns and live evals use real messages.

Existing T2 scenarios to treat as reference implementations (not duplicates to copy verbatim):

- `tests/evals/scenarios/tool_read.yaml` — core tools
- `tests/evals/scenarios/subagent_roundtrip.yaml` — subagents
- `tests/evals/scenarios/memory_write_inject.yaml` — memory injection
- `tests/evals/scenarios/context_curation.yaml` — context budget (scripted; live needs real token volume)

## Current state (verified against code, 2026-07-01)

**Reporting/persistence gaps:**
- `evals/runner.py` drives the live gateway over SSE and builds a `TurnResult` per turn
  ([models.py:19](../evals/models.py)) — but it only keeps `input`, `output`, `trace_id`,
  `scores`, `reasons`. The `TurnComplete` event's `usage` object (tokens, cost, duration) is
  parsed off the wire and thrown away at [runner.py:97](../evals/runner.py).
- `_collect_turn_output` only handles `AssistantDelta`, `Error`, and `TurnComplete`. It
  ignores `ToolCallStarted`, `ToolCallResult`, `ContextSummarized`, and other SSE events
  the gateway already emits ([events.py](../src/monkeybot/core/runtime/events.py)).
- `evals/store.py` is a plain in-memory dict behind a lock — a run is gone the moment the
  process restarts. There is no way to compare "this run" to "that run."
- `.github/workflows/ci.yml` has no eval step at all. Nothing runs live evals today, on any
  trigger.

**Scenario coverage gaps — this is the bigger risk.** `evals/scenarios/` has 3 YAMLs.
Mapped against the actual harness subsystems:

| Subsystem | Where it lives | Real failure modes | T2 (`tests/evals/`) | Live (`evals/`) |
|---|---|---|---|---|
| Core tools | [core_tool_executor.py](../src/monkeybot/core/tools/core_tool_executor.py) | wrong args, sandbox crash, workspace write failure | `tool_read.yaml` | none |
| MCP (external tools) | [mcp_client.py](../src/monkeybot/core/mcp/mcp_client.py) | `MCPConnectionError`, `MCPAuthError`, OAuth token refresh, server-not-connected | none | none — browser MCP ingress is untested new code |
| Skills | discovered from `SKILL.md` in [context/\_\_init\_\_.py:279](../src/monkeybot/core/context/__init__.py), invoked as a tool | skill not discovered, wrong skill picked, invocation silently no-ops | none | [skill_invocation.yaml](../evals/scenarios/skill_invocation.yaml) — judge only; prose passes with zero tool calls |
| Subagents | [worker_pool.py](../src/monkeybot/core/subagents/worker_pool.py) | stale claim, run recorded failed by wrong owner, run never completes | `subagent_roundtrip.yaml` | none |
| Memory | [memory/hook.py](../src/monkeybot/core/memory/hook.py) | write never promoted, recall lost across sessions | `memory_write_inject.yaml` (injection only) | [memory_recall.yaml](../evals/scenarios/memory_recall.yaml) — single session only |
| Context summarization | [context/curator.py](../src/monkeybot/core/context/curator.py) | truncation drops needed context | `context_curation.yaml` (scripted, no real threshold) | none — no scenario crosses the real token threshold |
| Providers/model config | [providers/](../src/monkeybot/core/providers) | provider-specific quirks (e.g. NVIDIA NIM) | N/A (fake provider) | none — runs against whatever provider is mounted at eval time |

So the honest gap is: no persistence, no baseline, no report, no CI hook for live evals — and
a live scenario suite that gives false confidence because it can't tell "the harness worked"
from "the model wrote something that sounded fine." T2 catches loop regressions cheaply; live
evals are what catches "works in pytest, breaks with a real model over SSE."

## What to build, in order

Do these in sequence — each step is a prerequisite for the next, and each should be
independently useful if the ticket gets cut short.

### 1. Capture full turn telemetry from SSE (usage + mechanism data)

This merges what were previously separate "stop dropping usage" and "add mechanism
assertions" steps — persist once the data model is complete, not halfway.

Extend `TurnResult` (and per-scenario aggregates) to carry everything the runner can derive
from SSE without a trace-backend client:

- From `TurnComplete`: `usage` (tokens, cost, `duration_ms`)
- From `ToolCallStarted` / `ToolCallResult`: tool name, args summary, errors
- From `ContextSummarized`: `summarizations_count` per turn
- Derived counts: `tool_calls_count`, `tool_calls_by_name`, `tool_errors_count`,
  `subagent_calls_count` (e.g. `task` tool), `llm_calls_count` if exposed on stream

Update `_collect_turn_output` to accumulate these events for the matching `request_id`
alongside assistant text. No other runner behavior changes.

This unlocks every cost/latency data point and every deterministic assertion below.

### 2. Add requirement-style assertions (not just judge scoring + caps)

Today a scenario's `assertions` block is only used to pick deepeval metrics. Caps like
`max_tool_calls` are listed in this plan but not enforced in code. Add both caps and
requirements, evaluated against the telemetry from step 1:

- **Caps (unchanged intent):** `min_score`, `max_latency_ms`, `max_input_tokens`,
  `max_output_tokens`, `max_tool_calls`, `max_tool_errors`
- **Requirements (new):** `required_tools: [list_skills]`, `min_tool_calls: 1`,
  `min_subagent_calls: 1`, `min_summarizations: 1` — fail if actual telemetry doesn't
  satisfy them, regardless of judge score

Reuse naming aligned with `tests/evals/` where possible (`tool_categories_used` in T2 maps
to `required_tools` or a `required_tool_categories` alias in live scenarios).

This is the fix for `skill_invocation.yaml` passing today with zero real tool calls.

### 3. Persist a run to disk after it completes

Write one JSON file per run (run metadata + per-scenario data + suite aggregate — see field
list below) under `evals/runs/`. Add `evals/runs/` to `.gitignore` — only baselines are
committed, never individual run artifacts.

Keep `store.py`'s in-memory store as-is for the live UI; the disk file is what PR
comparison reads.

Include `schema_version` at the top of every run file and baseline file so the report
command can evolve fields without breaking old baselines.

### 4. Reorganize scenarios into one folder per subsystem, and fill the empty ones

Don't build a coverage-tracking framework — one scenario per folder with a real requirement
assertion (from step 2) is the bar, not exhaustive path coverage.

```text
evals/scenarios/
  tools/        # a core tool is called and succeeds; one deliberate-error case
  mcp/          # see MCP strategy below — may ship as local-only first
  skills/       # skill_invocation.yaml, moved here, gains required_tools assertion
  subagents/    # min_subagent_calls: 1 (mirror tests/evals/subagent_roundtrip.yaml intent)
  memory/       # memory_recall.yaml (existing) + cross-session write-then-recall
  context/      # long enough to cross summarization threshold; min_summarizations: 1
  multi_turn/   # multi_turn_task.yaml, moved here (already solid, no change needed)
```

**Memory cross-session:** `run_scenario_live` currently opens exactly one session per
scenario ([runner.py:104](../evals/runner.py)). Add minimal support for a scenario to span
two sessions against the **same workspace** — not a general session-orchestration DSL, just
enough to express "these messages, then a new session, then this probe." CI must use a
stable workspace path (e.g. `demo_agent/workspace` or a temp dir wired through agent config),
not ephemeral `/tmp` that vanishes between sessions.

**MCP strategy (hardest folder):** live MCP scenarios need a real MCP server in the
environment. Two-phase approach:

1. **This ticket:** add `mcp/` with one scenario that asserts an MCP tool was invoked, runnable
   locally against `demo_agent` with browser MCP (or another configured server). Document
   required agent config in the scenario README comment.
2. **CI:** either spin up a minimal in-process MCP fixture (similar to
   `integrations/browser-mcp` pytest) as a workflow service, or mark `mcp/` scenarios
   `tags: [local-only]` and exclude them from the smoke suite until fixture exists. Do not
   block the rest of the ticket on full MCP CI.

### 5. Smoke suite manifest + baseline diff + report command

**Smoke suite** — explicit list in `evals/suites/smoke.yaml` (or `.json`), not implicit
"all scenarios in tree." Initial smoke membership (~7 scenarios, one per folder except MCP
may be deferred):

```yaml
# evals/suites/smoke.yaml
id: smoke
scenarios:
  - tools/core_read
  - skills/skill_invocation
  - subagents/dispatch_complete
  - memory/recall_single_session
  - memory/recall_cross_session
  - context/summarization_trigger
  - multi_turn/task_tracking
  # - mcp/tool_invoke   # add when CI fixture exists
```

**Baseline:** one committed file `evals/baselines/smoke.json` with `schema_version`,
per-scenario aggregates, and suite-level totals. Refreshed manually only when the team
agrees a change is better — never auto-updated by CI.

**Report command:**

```bash
uv run python -m evals.report --suite smoke --baseline evals/baselines/smoke.json
```

Reads the latest run under `evals/runs/` (or `--run <path>`) + baseline, prints the
Markdown report (template below). `--fail-on-regression` makes it usable as a gate.

### 6. Resolve gate policy for judge flakiness (before wiring CI)

deepeval/LLM-judge scores are non-deterministic run-to-run. **Decide this before step 7** —
don't ship a gate people learn to ignore.

| Check type | Gate behavior |
|---|---|
| Scenario errors | **Hard fail** |
| Requirement assertions (`required_tools`, `min_subagent_calls`, `min_summarizations`, etc.) | **Hard fail** (deterministic on SSE telemetry) |
| Cap assertions (`max_tool_errors`, `max_latency_ms`, etc.) | **Hard fail** |
| Cost / latency / token deltas vs baseline | **Hard fail** (thresholds below) |
| Judge quality scores vs baseline | **Warn only** in v1 — surface in report, do not block merge |
| Quality improves but cost rises | **Warn only** — author explains tradeoff in PR |

Rationale: requirement assertions carry the harness-regression signal; judge scores are useful
for human review and baseline trending but too noisy for an automated merge gate in v1. If
judge gating is needed later, median-of-2-runs or widened thresholds can be added without
changing the scenario schema.

### 7. Wire it into CI (prerequisites + two triggers)

Live evals in CI are the capstone, not the foundation. Get steps 1–5 working locally first.

#### CI prerequisites

Before adding the workflow job, document and implement:

1. **Agent boot:** start monkeybot gateway in the workflow (e.g. `uv run monkeybot serve`
   against `demo_agent/` or a minimal eval fixture config) and wait for `/health`.
2. **Secrets:** provider API key for the agent model + judge model (today `evals/scorer.py`
   uses deepeval/Gemini — pin `JUDGE_PROVIDER` / `JUDGE_MODEL` env vars in the workflow).
3. **Workspace:** stable `WORKSPACE_PATH` for memory cross-session scenarios.
4. **Cost budget:** smoke suite ≈ 7 scenarios × 2–4 turns × (agent + judge) calls. Estimate
   per-run cost in the workflow README comment; expect ~$0.50–2.00/run depending on model.
5. **Artifacts:** upload run JSON + Markdown report on failure; do not commit `evals/runs/`.

#### Triggers (not every PR)

LLM-judge calls cost money and take time. Run smoke with `--fail-on-regression` on:

- **Any PR from `develop` into `main`.** Release-promotion gate. Scope with
  `base_ref == main` / `head_ref == develop` so it does not fire on feature-branch PRs into
  `develop`.
- **Any PR that touches dependency files** — `uv.lock`, `pyproject.toml`,
  `cli/pyproject.toml`, `cli/uv.lock`, `demo_agent/pyproject.toml`, `demo_agent/uv.lock`.
  Copy the `paths:` filter pattern from `publish-release.yml`. Catches silent SDK breakage
  (e.g. provider request/response shape changes) that code review won't see.

Both post the same Markdown report as a PR comment or check-run summary. A larger/nightly
suite is a later addition — not part of this ticket.

### 8. Trace URL fetching (deferred)

Only after the above is in daily use and someone asks for span-level drill-down. Derive
tool/LLM/subagent counts from SSE events (step 1); don't add a Langfuse/Phoenix API client
speculatively.

## How the dev team actually uses this

- **Local, on demand, anytime — no CI required.** A dev runs the same command CI runs:

  ```bash
  uv run python -m evals.report --suite smoke --baseline evals/baselines/smoke.json
  ```

  Primary loop while working on a harness change: run before opening a PR, read the Markdown
  report, iterate. Needs a running agent (`AGENT_URL`) and the baseline file only.
- **CI runs automatically only at the two trigger points above** with `--fail-on-regression`,
  blocking merge on hard-fail dimensions (requirements, caps, cost/latency, errors) but
  warning on judge noise. Everywhere else (feature branch → develop) it's opt-in locally.
- **Refreshing the baseline is a manual, reviewed act** — run the report, review deltas,
  commit updated `evals/baselines/smoke.json` in its own PR. CI never overwrites it.
- **Reading a failure:** report shows which dimension moved (requirements / tokens / cost /
  latency / tool errors / judge warnings) and which scenario, plus trace link when
  available.

## Data to capture per run

Run metadata: `schema_version`, `run_id`, `suite`, `git_sha`, `git_branch`,
`dirty_worktree`, `started_at`, `finished_at`, `agent_url`, `model_provider`, `model_name`,
`judge_provider`, `judge_model`.

Per scenario: `scenario_id`, `description`, `tags`, `status` (passed/failed/errored),
`messages_count`, `quality_scores`, `quality_reasons`, `pass_rate`, `latency_ms_total`,
`latency_ms_by_turn`, `input_tokens`, `output_tokens`, `cached_tokens`, `cache_read_tokens`,
`cache_creation_tokens`, `cost_usd`, `tool_calls_count`, `tool_calls_by_name`,
`tool_errors_count`, `llm_calls_count`, `summarizations_count`, `subagent_calls_count`,
`trace_ids`, `failure_reason`, `requirement_failures` (list of which assertions failed).

Suite aggregate: scenario count, pass/fail/error count, mean and p95 latency, total tokens,
total cost, mean quality score by metric, worst regressed scenarios, best improved scenarios,
`warnings` (judge deltas that did not block).

**Baseline file** mirrors suite aggregate + per-scenario summary fields (not full turn text).
Include `schema_version` and `recorded_at` + `git_sha` of the run it was captured from.

## Regression gates

Fail the PR gate (`--fail-on-regression`) when:

- Any required scenario errors
- Any requirement or cap assertion in scenario YAML fails
- Any hard-fail baseline delta (below)

Warn only (report, no block) when:

- Judge quality score drifts vs baseline
- Quality improves but cost/latency/tokens rise

### Per-scenario YAML gates

- **Caps:** `min_score`, `max_latency_ms`, `max_input_tokens`, `max_output_tokens`,
  `max_tool_calls`, `max_tool_errors`
- **Requirements:** `required_tools`, `min_tool_calls`, `min_subagent_calls`,
  `min_summarizations`

### Baseline comparison (hard fail)

| Dimension | Default gate |
|---|---|
| Pass rate (requirement + cap assertions) | fail if drops by `> 0` (any scenario that passed baseline now fails) |
| Total tokens | fail if increases by `> 15%` |
| Output tokens | fail if increases by `> 20%` |
| Cost | fail if increases by `> 15%` |
| p95 latency | fail if increases by `> 20%` |
| Tool errors | fail if count increases above baseline |
| Scenario errors | always fail |

### Baseline comparison (warn only, v1)

| Dimension | Default gate |
|---|---|
| Mean judge quality score | warn if any metric drops by `> 0.05` |
| Pass rate (judge-only scenarios) | warn if drops by `> 0.05` |

## Report template

```markdown
## monkeybot Eval Scorecard

Run: `<run_id>`
Commit: `<git_sha>`
Suite: `<suite>`
Model: `<model_provider>/<model_name>`
Judge: `<judge_provider>/<judge_model>`

### Verdict

`PASS` / `FAIL`

### Summary

| Metric | Baseline | Current | Delta |
|---|---:|---:|---:|
| Scenarios passed |  |  |  |
| Mean quality (informational) |  |  |  |
| Total tokens |  |  |  |
| Cost |  |  |  |
| p95 latency |  |  |  |
| Tool errors |  |  |  |

### Hard failures

- None, or list scenario + failed requirement/cap + trace link.

### Warnings (non-blocking)

- None, or list judge/cost tradeoff notes.

### Improvements

- List only material wins.

### Trace Links

- `<scenario_id>`: `<trace_url>`
```

## Non-goals (don't build these yet)

- No custom dashboard — Markdown reports until they become painful to read.
- No database for eval runs — disk JSON until that becomes insufficient.
- No committing `evals/runs/` — gitignored; only `evals/baselines/` is versioned.
- No synthetic scenario generator — hand-written scenarios until they stop catching
  regressions.
- No single "overall score" — keep quality/cost/latency/tokens visible separately so
  tradeoffs can't hide inside one number.
- No trace-backend client (Langfuse/Phoenix API reads) until someone asks for span
  drill-down that SSE can't answer.
- No general multi-session orchestration DSL — memory cross-session needs one
  open-close-reopen sequence, nothing more general.
- No exhaustive per-subsystem test matrix — one real scenario per folder is the bar, not
  100% path coverage of MCP/subagents/tools.
- No duplicate of `tests/evals/` — T2 stays in pytest; live evals add E2E coverage only.
- No judge-score merge gate in v1 — requirements + cost/latency are the automated gate;
  judge is informational until flakiness strategy is proven.

## Definition of done

- A PR author can run one command locally, anytime, and get a Markdown report comparing
  their branch to the committed baseline — no CI dependency.
- `evals/runner.py` captures full turn telemetry from SSE (usage, tool calls, summarizations).
- Requirement assertions fail scenarios when the harness didn't fire, independent of judge score.
- `evals/runs/` is gitignored; `evals/baselines/smoke.json` includes `schema_version`.
- `evals/suites/smoke.yaml` explicitly lists smoke scenarios.
- CI runs smoke with `--fail-on-regression` on (a) `develop` → `main` PRs and (b) lockfile
  manifest PRs, with agent boot + secrets documented. Posts the same report. Does not run
  on ordinary feature-branch PRs.
- Baseline only changes via explicit, reviewed commit — never auto-updated by CI.
- `evals/scenarios/` organized into subsystem folders; each has at least one scenario with
  a requirement-style assertion (not judge-score-only).
- `skills/skill_invocation.yaml` fails if no tool call actually happened.
- At least one `memory/` scenario proves recall survives a new session (stable workspace).
- At least one `context/` scenario triggers real summarization and asserts `min_summarizations: 1`.
- `mcp/` scenario exists for local runs; smoke suite documents whether MCP is included or
  deferred pending CI fixture.
- Gate policy documented: requirements/caps/cost = hard fail; judge = warn in v1.
