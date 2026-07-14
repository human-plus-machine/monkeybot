# Transcript analysis & harness improvement plan

Session transcripts are opt-in NDJSON logs for **harness debugging** (not agent-facing). This plan covers:

1. What we capture today
2. Auto analysis on session end (`/bye` / Ctrl+C) that writes a report folder
3. Transcript gaps to fill so we can later build a self-improving feedback loop

**Out of scope for this plan:** a fully closed loop that auto-labels outcomes, proposes patches, or merges harness changes without a human in the loop.

---

## Current state

### Config

| Knob | YAML | Env | Default |
|------|------|-----|---------|
| Enable transcripts | `runtime.transcript_enabled` | `MONKEYBOT_TRANSCRIPT_ENABLED` | `false` |
| Include live SSE deltas | `runtime.transcript_include_live` | `MONKEYBOT_TRANSCRIPT_INCLUDE_LIVE` | `false` |

Smoke agent enables transcripts by default (`evals/smoke_agent/monkeybot_config/monkeybot.yaml`).

### Location & format

- **Today:** `{workspace_root}/.monkeybot/transcripts/{session_id}.ndjson` (flat file)
- **Planned:** one folder per session (see Part 1) so the NDJSON and analysis artifacts live together
- **Format:** append-only NDJSON, one JSON object per line
- **Writer:** `src/monkeybot/core/persistence/transcript.py` (`TranscriptWriter`)
- **Wiring:** gateway SSE turn loop + provider I/O in `core/runtime/loop.py`

### Record types today

| Type | Role |
|------|------|
| `SessionManifest` | Once per file: session id, model, provider, workspace, `agent_md`, `durable_only` |
| `UserMessage` | Incoming user turn (`request_id`, `content`) |
| `ProviderRequest` | What we sent the model (delta-encoded messages; tools on turn 1 / reset / tool change) |
| `ProviderResponse` | Settled text, thinking, `tool_requests`, token usage |
| Durable `AgentEvent`s | Same shape as SSE: `ToolCallStarted` / `ToolCallResult`, `TurnComplete`, `Error`, `ContextSummarized`, etc. |

Live-only events (`AssistantDelta`, `SystemPromptSnapshot`, …) are skipped unless `transcript_include_live` is on.

### Parallel tooling (not wired to transcripts)

| Layer | Artifact | Notes |
|-------|----------|-------|
| Live evals | `evals/runner.py`, `evals/report.py`, baselines | SSE only; no transcript path on run JSON |
| Deterministic evals | `tests/evals/` + `FakeProvider` | No transcript integration |
| OTEL | `monkeybot[observability]` | Per-LLM / per-tool spans; `TurnComplete.trace_id` when enabled |

There is **no transcript analyzer** in the repo today. Manual NDJSON inspection (and ad-hoc analysis documented in `docs/progressive-mcp-tools.md`) is the current workflow.

---

## Goal

Give engineers and coding agents a **fast, structured way to turn a session transcript into harness hypotheses**: what’s slow, what’s broken, and what to change next — without reading raw NDJSON by hand.

**Primary UX:** when the user ends a chat session (`/bye`, `/quit`, `/exit`, or Ctrl+C), if transcripts are enabled, the harness automatically analyzes that session and writes `brief.md` / `report.json` / `meta.json` next to `transcript.ndjson` in the same time-sortable session folder. No manual CLI args required for the normal workflow.

Later (after the gaps below), the same artifacts can feed a human-supervised improvement loop: eval fail → open auto-generated report → patch → re-run smoke.

---

## Part 1 — Auto analysis on session end (build first)

Library + teardown hook. Suggested placement: analyzer module under `src/monkeybot/core/persistence/` or `src/monkeybot/diagnostics/`, invoked from CLI session exit paths (and optionally gateway session close).

### Trigger

Run analysis automatically when a session ends and transcripts were enabled for that session:

| Exit path | Where to hook today |
|-----------|---------------------|
| `/bye`, `/quit`, `/exit` | CLI chat / TUI exit (`is_exit_command`, `_goodbye_and_exit`) |
| Ctrl+C / SIGINT | Same `finally` / close path after interrupt is set |
| Controller close | After `controller.close()` |

**Important distinction — CLI exit vs. gateway session end.** `controller.close()` (`ChatSessionController.close()` in `chat_session.py`) only tears down the CLI's local HTTP client; it does not call `DELETE /sessions/{id}` on the gateway, and the `TranscriptWriter` / NDJSON file live in the **gateway process**, addressed relative to the gateway's `workspace_root` — not the CLI's. So:

- With `--attach` (CLI connects to an already-running gateway it didn't spawn), exiting the CLI does nothing to the gateway session; the transcript may still be open in a separate process the CLI has no privileged access to, possibly on a different host.
- Even in the common "spawned gateway" case, analysis should live **in the gateway process**, near `TranscriptWriter` / `workspace_root` — triggered off the `DELETE /sessions/{id}` handler (`routes.py`) or gateway process shutdown — not bolted onto the CLI's `/bye` handler. The CLI's `/bye` should at most be the signal that triggers session deletion; it shouldn't own the analysis code.
- Also, `TranscriptWriter._append_line` already opens/closes the file on every write (`with self._path.open("a")`), so each line is durable as soon as it's written — there's no buffering to "flush" on close. Don't add flush-on-close logic under this assumption.
- SIGINT/crash on the **gateway** process itself (not the CLI) has no guaranteed teardown hook today for running analysis. This is a real gap for Part 1 to account for, even though we're not building an automatic retry/backfill mechanism for it right now.

Behavior:

1. User ends session
2. Session / gateway teardown completes (transcript file closed)
3. Analyzer loads the session folder’s NDJSON (see layout below)
4. Writes `brief.md`, `report.json`, and `meta.json` into that same folder
5. Prints a one-line pointer in the terminal, e.g. `Transcript report → workspace/.monkeybot/transcripts/{session_dir}/`

Skip silently (or log debug) when transcripts are off or the NDJSON file is missing / empty.

### Session folder layout

Colocate the raw transcript and analysis artifacts in one directory per session:

```text
{workspace_root}/.monkeybot/transcripts/
├── 20260714T154201Z_a1b2c3d4/          # newest session (lexicographic / reverse sort)
│   ├── transcript.ndjson              # append-only capture (SessionManifest + events)
│   ├── brief.md                       # agent/human-readable debug brief
│   ├── report.json                    # machine-readable scorecard + findings
│   └── meta.json                      # session_id, started_at, analyzed_at, harness fingerprint
├── 20260714T141100Z_e5f6g7h8/
│   ├── transcript.ndjson
│   ├── brief.md
│   ├── report.json
│   └── meta.json
└── …
```

**Folder naming (time-sortable):** `{UTC_compact}_{sanitized_session_id}/`

- Example: `20260714T154201Z_a1b2c3d4/`
- UTC compact = `YYYYMMDDTHHMMSSZ` from session start (same instant as `SessionManifest.started_at`)
- Lexicographic **descending** sort (`ls -r` / Finder sort Z→A) puts the **latest session on top**
- `session_id` remains in the suffix and inside `meta.json` / manifest for correlation with the gateway

**Migration note:** today’s flat `{session_id}.ndjson` becomes `…/transcripts/{time}_{id}/transcript.ndjson`. `TranscriptWriter` should create the session directory on first write; analysis only adds the sibling report files on exit.

Keep the `transcripts/` tree local / gitignored unless the team decides otherwise. Re-analyze overwrites `brief.md` / `report.json` / `meta.json` in place; do not rewrite `transcript.ndjson` during analysis.

### Capabilities (same analyzer core)

#### 1. Parser + session model

- Load NDJSON; reconstruct delta-encoded `ProviderRequest` messages into full conversation state
- Group by `request_id` (outer user turn) and `inner_turn` (tool loop)
- Build timeline: `UserMessage` → `(ProviderRequest → events → ProviderResponse)*` → `TurnComplete`
- Resolve transcript path from the active session’s workspace + session id (same sanitize rules as `TranscriptWriter`)

#### 2. Performance profile

Derive from existing `ts` fields (and richer timing once Part 2 lands):

| Metric | Source |
|--------|--------|
| Wall time per user turn | `UserMessage.ts` → matching `TurnComplete.ts` |
| LLM call latency | `ProviderRequest.ts` → `ProviderResponse.ts` per `inner_turn` |
| Tool latency | `ToolCallStarted.ts` → `ToolCallResult.ts` (match on `call_id`) |
| Time in tools vs model | Sum of tool spans vs sum of LLM spans |
| Context pressure | Message / char estimates before each request; `ContextSummarized` |
| Tool-schema bloat | `len(tools)` + schema char size on turn-1 / tool-change requests |

Write a scorecard into `report.json`: slowest turns, slowest tools, token totals, inner-turn count, error rate.

**Caveat:** `TurnComplete` fires from a `finally` block in `core/runtime/loop.py` that also runs on cancellation/error, so a cancelled turn still gets a `TurnComplete` record with a real (if truncated) duration. "Wall time per user turn" and any pass/fail framing need a companion signal — e.g. check whether an `Error` event preceded that `TurnComplete` — so cancelled turns aren't miscounted as normal completions.

#### 3. Harness smell detectors

Heuristic findings with evidence pointers (`seq`, record `type`, short snippet):

- **Tool loop thrash** — same tool + similar args repeated N times
- **Empty post-tool replies** — `ProviderResponse` with no text after tools (silent continue / retry path)
- **Orphan tool calls** — `tool_requests` without matching `ToolCallResult`
- **Policy vs execution** — security / allowlist language in `ToolCallResult.error`
- **Context churn** — frequent `ContextSummarized` / `messages_reset`
- **Progressive tools** — tools list growing/shrinking mid-session without clear success
- **Token waste** — huge system / tool schemas vs short user content
- **Error clustering** — repeated identical error strings

#### 4. Agent-facing debug brief (`brief.md`)

```text
## Session summary
## Timeline (compressed)
## Perf hotspots
## Suspected harness issues (ranked)
## Exact evidence (seq + type + snippet)
## Suggested next probes (files / config knobs)
```

Paste `brief.md` into Cursor / Slack, or open the folder after `/bye`.

### Optional: manual re-run (secondary)

A thin library entrypoint / internal helper is fine for CI and re-analysis of an existing NDJSON (e.g. after pulling a failed smoke artifact). **Primary product UX is automatic on session end** — no required CLI args for day-to-day use.

Eval linkage: once Part 2 records `session_id` / transcript path on eval runs, the same analyzer can write a report folder for that session when the eval process tears down.

### Config

| Knob | Suggestion |
|------|------------|
| Gated by transcripts | Only run when `transcript_enabled` / `MONKEYBOT_TRANSCRIPT_ENABLED` is on |
| Optional kill switch | e.g. `runtime.transcript_report_on_exit` (default `true` when transcripts on) if someone wants capture without reports |

### Explicit non-goals for v1

- Do not auto-patch the harness
- Do not inject findings into the live agent context
- Do not require users to pass CLI args for normal chat sessions
- Do not require OTEL to be enabled (use transcript `ts` first; enrich when spans exist)
- Do not block `/bye` for long — keep analysis fast / sync-but-cheap; if it ever gets heavy, run in a short background step with a clear “report pending” message

---

## Part 2 — Transcript gaps (enable stronger analysis & a later loop)

Today’s transcript is strong for **correctness replay**, weak for **perf, policy, and causality**. Fill these before investing in automation.

### Must-have

| Gap | Why |
|-----|-----|
| Populate `TurnComplete.usage.duration_ms` | Field exists but the loop leaves it `0`; eval `max_latency_ms` and analyzers will be wrong until fixed |
| Per-span timing on durable events | `duration_ms` (or start/end) on LLM and tool spans; wall-clock `ts` diffs are noisier and miss TTFT |
| TTFT / first-token | Time from `ProviderRequest` to first durable assistant / thinking boundary (provider slowness vs harness) |
| Inspector decisions as durable events | e.g. `InspectorDenied` with inspector name, rule, tool, args hash — policy vs execution |
| Session ↔ eval linkage | Write `session_id` + transcript path into `evals/runs/*.json`; optional `eval_run_id` on manifest |
| Harness fingerprint on manifest | Git sha / package version, model, hashes of allowlist / mcp.json / AGENT.md — attribute regressions |

### Should-have

| Gap | Why |
|-----|-----|
| Hook / memory / curation decisions | What was injected, summarized, or dropped (mostly invisible today) |
| MCP / progressive tool activation | When tools were added/removed and why |
| Subagent child transcript refs | Parent `task` result + path/id of child `.ndjson` |
| Cost on `ProviderResponse.usage` | Tokens alone are incomplete for optimization goals |
| Explicit recovery events | Continued after empty response, `max_turns` pressure, retries |
| Realtime gateway parity | Realtime path barely tees provider I/O / events today |

---

## Target feedback loop (human-supervised, later)

```text
Live eval / real session
        │
        ▼
.monkeybot/transcripts/{time}_{session_id}/transcript.ndjson
        │
        ▼
session end (/bye, Ctrl+C, eval teardown)
        │
        ▼
same folder ← brief.md + report.json + meta.json
        │
        ▼
Human / coding agent opens latest session folder (newest first)
        │
        ▼
Harness / config patch
        │
        ▼
evals/report + baseline diff
(pass → promote; fail → re-open latest session folder)
```

Prerequisite pieces we already have: live evals, assertions, `evals/diff`, optional OTEL, smoke agent with transcripts on. Missing middle piece: **on-exit analysis written next to the NDJSON, with time-sortable session folders**.

---

## Priority order

| Priority | Work |
|----------|------|
| **P0** | Change transcript layout to `{time}_{session_id}/transcript.ndjson` (time-sortable dirs, latest first when sorted descending) |
| **P0** | Analyzer library: parse, timeline, LLM/tool latency from `ts`, tool-schema size, smell detectors, `brief.md` + `report.json` |
| **P0** | Wire auto-run on CLI session end (`/bye` / Ctrl+C) → write reports into that same session folder |
| **P0** | Fix `duration_ms` on `TurnComplete` |
| **P1** | `session_id` / transcript path on eval run records; run analyzer on eval teardown too |
| **P1** | Durable inspector deny events + per-tool `duration_ms` |
| **P2** | Manifest fingerprint, subagent transcript links, TTFT |

---

## Key code references

| Symbol / area | Path |
|---------------|------|
| `TranscriptWriter` | `src/monkeybot/core/persistence/transcript.py` |
| Durable event kinds | `src/monkeybot/core/runtime/events.py` |
| Provider request/response tee | `src/monkeybot/core/runtime/loop.py` |
| Gateway event tee | `src/monkeybot/gateway/sse/app.py` |
| Config → env map | `src/monkeybot/core/config/runtime_env.py` |
| Exit commands (`/bye`, …) | `cli/src/monkeybot_cli/exit_commands.py` |
| Chat exit / Ctrl+C | `cli/src/monkeybot_cli/commands/chat.py`, `chat_tui.py` (`_goodbye_and_exit`) |
| Live eval runner / report | `evals/runner.py`, `evals/report.py` |
| OTEL spans | `src/monkeybot/observability/spans.py` |
| Example manual transcript analysis | `docs/progressive-mcp-tools.md` (appendix) |

---

## Open questions for the team

1. Filename for the NDJSON: `transcript.ndjson` (proposed) vs `{session_id}.ndjson` inside the folder?
2. Analyze only on clean `/bye`, or also on Ctrl+C / crashy disconnects (best-effort)?
3. Do we always capture durable timing in transcripts, or keep rich timing behind an opt-in flag to limit file size?
4. For inspector events: log denials only, or allow + deny (volume vs debug value)?
5. Should smoke CI archive whole session folders (NDJSON + reports) on failure?
6. Does analysis run inside the gateway process (co-located with `TranscriptWriter` and `workspace_root`), or a separate short-lived process reading the same NDJSON file? This determines where the analyzer module can actually live and whether `--attach` sessions can be analyzed at all from the CLI side.
