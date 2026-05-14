# Design: monkeybot-v2-e4 — Subagents, Durability & LLM Council
## Phase 1A: Discovery & Core Design

---

## Executive Summary

E4 gives monkeybot the ability to spawn isolated child processes (subagents) for complex multi-step pipelines, survive container crashes via `DurableRunStore`, and grow smarter over time through an LLM Council that writes structured memory files after each session. Subagents can be **declared by the operator** in `config.yaml` (with their own scripts, skills, and models) and/or **spawned dynamically** by the main agent LLM at runtime. All capabilities bolt onto the existing `AgentLoop` + `events.py` infrastructure from E1–E3 with no new required runtime dependencies.

---

## Use Case & Business Value

**US-09 — Durable Subagent Pipelines:** A bot operator runs a research → draft → review → post pipeline across multiple subagent processes. If the Cloud Run container crashes mid-pipeline, `pending_runs()` surfaces the interrupted run so the operator (or a recovery script) can resume it — no data is lost and no manual SQLite spelunking is required.

**FR-015 — Subagent Protocol:** A parent agent sends a `SubagentEnvelope` (context + task) to a child script via stdin and reads `AgentEvent` JSON lines from the child's stdout. The parent's async iterator yields those events as if they came from its own loop — fully composable.

**FR-017 — LLM Council Memory:** After each session, the Council reads the full conversation and writes structured `.md` memory files. The next session starts with richer context; the agent gets smarter without manual curation.

**New — Operator-Declared Subagents:** An operator can register named subagents in `config.yaml`, each with its own script, skills directory, model override, and description. The main agent's system prompt is enriched with the registry so the LLM knows what specialized agents are available to delegate to. The main agent can still spawn any subagent ad-hoc by script path.

---

## Technical Context (from codebase inspection)

| Aspect | Current State |
|---|---|
| Language / runtime | Python 3.11+, `asyncio`-native |
| DB | SQLite via `aiosqlite`; WAL mode; `sqlite:///data/monkeybot.db` |
| Existing tables | `messages`, `turn_usage`, `job_runs` |
| Event stream | `events.py` already defines `SubagentStarted`, `SubagentCompleted`, `event_to_json()`, `event_from_json()` |
| `AgentLoop.run()` | Async generator yielding `AgentEvent`; `_on_turn_complete` callback already wired |
| ID strategy | ULID throughout — follow the same |
| Memory | `core/memory.py` — `save_memory(memory_path, filename, content)` writes `{filename}.md` |
| Config | `bots/example-bot/config.yaml` — YAML dict loaded at startup, passed to `AgentLoop` as `config: dict[str, Any]` |
| Skills | `.agents/skills/` — markdown files; `skills_path` in config points to the directory |
| Optional deps | `[scheduler]` extra shows the pattern for optional deps |

**Key insight:** `events.py` already has the full subagent event vocabulary (`SubagentStarted`, `SubagentCompleted`) and the serialization helpers (`event_to_json` / `event_from_json`). E4 only needs to implement the *mechanics* (process spawn + stdin/stdout wiring), the *registry* (declared subagent definitions), and the *persistence* (durable run store + council).

---

## Architecture Decision

### Chosen Approach

**stdin/stdout JSON-line transport + subagent registry + shared SQLite for durability + async background council.**

- **`core/subagent_registry.py`** — Loads the `subagents` block from `config.yaml` into a dict of `SubagentDefinition` dataclasses. Provides `resolve(name)` (returns the definition for a named subagent) and `all_definitions()` (used to build the system prompt). Raises `KeyError` for unknown names.
- **`core/subagent_proto.py`** — Defines `SubagentEnvelope` (the payload sent to a child), `spawn_subagent()` (async generator: spawns the subprocess, writes the envelope to stdin, yields `AgentEvent` objects from stdout), `read_envelope_from_stdin()` (used inside the child script), and `emit_event()` (child calls this to write a JSON-line to stdout). `spawn_subagent()` accepts either a registered `SubagentDefinition` or a raw script path for ad-hoc spawns.
- **`core/runs.py`** — Stateless helpers: `create_scratch_dir(run_id)` creates an isolated temp dir for a run, `cleanup_old_runs(base_dir, max_age_days)` prunes stale dirs.
- **`core/durable_runs.py`** — `DurableRunStore` class (mirrors `ConversationHistory` in style): `record_started()`, `record_completed()`, `record_failed()`, `pending_runs()`. Writes to the existing SQLite DB.
- **`subagents/researcher.py`** — A self-contained example subagent: reads its `SubagentEnvelope` from stdin (including `skills_path` from the envelope context), runs a minimal research loop, emits events, finishes with `TurnComplete`.
- **`core/council.py`** — `run_council()` async function: takes conversation text + memory path, calls the configured LLM provider with `COUNCIL_PROMPT`, parses the structured response, and writes one or more `.md` files via `save_memory()`.
- **Council hook** — Wired as `asyncio.create_task()` in the `_on_turn_complete` callback on `AgentLoop`, so it never blocks the main event loop.
- **System prompt enrichment** — At `AgentLoop` construction time, `SubagentRegistry.all_definitions()` is serialized as a markdown block and appended to the system prompt, so the main LLM knows what named subagents exist and what each is for.

### Alternatives Considered

| Option | Pros | Cons | Why Not Chosen |
|---|---|---|---|
| Unix socket / HTTP transport between parent and child | More flexible (bidirectional, streaming control) | Complex setup; port management; harder to test | stdin/stdout is simpler, battle-tested (LSP, pytest-xdist), no network ports needed |
| Separate SQLite DB for durable runs | Clean isolation | Another file to manage; can't join with session history | Single DB is the established pattern; a separate `durable_runs` table is sufficient |
| Thread-based process spawn (blocking) | Avoids asyncio complexity | Blocks event loop; can't yield events asynchronously | `asyncio.create_subprocess_exec` is the natural fit for an async-first codebase |
| Council as a synchronous post-turn call | Simpler call site | Adds latency to every turn (LLM round-trip blocks next turn) | `asyncio.create_task()` keeps it fire-and-forget; memory is eventually consistent |
| Separate memory DB instead of `.md` files | Query-friendly | Breaks existing `search_memory()` keyword search pattern | `.md` files via `save_memory()` match the current memory interface exactly |
| Registry as a separate YAML file (not config.yaml) | Clean separation | Another file to manage; config is already the right place | Bot config and subagent config are deployed together — one file is simpler |
| Enforce that only registered subagents can be spawned | Strong access control | Too restrictive; ad-hoc spawns are needed for dynamic pipelines | Registry is opt-in; ad-hoc by script path remains available |

### ADR-001: stdin/stdout JSON-line protocol for subagent communication

**Status:** Accepted  
**Context:** E4 requires a parent process to send a task envelope to a child script and receive a stream of `AgentEvent` objects back. The events are already JSON-serializable via `event_to_json()` / `event_from_json()`.  
**Decision:** Parent writes one JSON line (the `SubagentEnvelope`) to the child's stdin. Child writes one `AgentEvent` JSON line per event to stdout. Parent reads stdout line-by-line, deserializes with `event_from_json()`, and yields each event.  
**Consequences:**
- Positive: Zero new dependencies; trivially testable with a tiny echo script; composable with any language.
- Negative: stdout must be exclusively used for event lines — child cannot use `print()` for debug output (must use stderr or a log file).
- Risk: If child writes a malformed line, parent emits an `ErrorEvent` and closes the subprocess — handled gracefully.

### ADR-002: `DurableRunStore` writes to the shared SQLite DB

**Status:** Accepted  
**Context:** The existing DB already has WAL mode and proven reliability. Adding a `durable_runs` table is a one-migration addition.  
**Decision:** `DurableRunStore.__init__(db_path)` matches `ConversationHistory.__init__(db_url)`. `init()` creates the `durable_runs` table. All lifecycle methods use `aiosqlite`.  
**Consequences:**
- Positive: Consistent with existing patterns; single backup target; can join `durable_runs` with `messages` by `session_id`.
- Negative: Shared DB means a schema migration is needed — mitigated by `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE … ADD COLUMN IF NOT EXISTS` guards.

### ADR-003: Council fires once per session via a configurable idle timer

**Status:** Accepted  
**Context:** The council must run at a natural session boundary, not after every single turn. Running after every turn wastes LLM calls (a 10-turn conversation would trigger 10 council calls) and produces no additional value since the read-merge-write pattern would yield the same output regardless. Fixed timers (e.g. 2 minutes) are too short for users who pause to read long responses before replying.  
**Decision:** `_on_turn_complete` maintains a per-session `asyncio.Task` that sleeps for `council.idle_seconds` (default: 300). Every incoming `TurnComplete` for a session cancels the existing timer and starts a fresh one. When the timer finally expires — meaning no new turns arrived for `idle_seconds` — `run_council()` fires once for the full session. This is a standard debounce pattern. On SIGTERM, all pending timers are flushed immediately (cancelled sleep, council runs synchronously) so no session loses its memory write.  
**Consequences:**
- Positive: One LLM call per session regardless of turn count. Natural session boundary. Zero latency impact on any turn. Configurable by operator to match their users' reading pace.
- Negative: If the process crashes mid-sleep (not on SIGTERM), the council for that session is lost. Mitigation: best-effort by design; one missed session is acceptable.
- Risk: Operator sets `idle_seconds` too low → council fires mid-conversation. Mitigation: document that 300s (5 min) is the recommended minimum for interactive bots.

### ADR-004: Subagent timeout = 300 seconds

**Status:** Accepted  
**Context:** PRT open item: "Confirm subagent timeout: parent should emit `ErrorEvent` if no `TurnComplete` is received within N seconds — define N."  
**Decision:** Default timeout = 300 seconds (5 minutes). Configurable globally via `config.yaml` key `subagents.timeout_seconds` and overridable per-definition via `SubagentDefinition.timeout_seconds`. If the child process does not emit `TurnComplete` within the timeout, parent terminates the subprocess and yields `ErrorEvent(message="Subagent timeout", recoverable=True)`.  
**Consequences:**
- Positive: Prevents hung subagents from blocking the parent indefinitely.
- Negative: Long-running research tasks (web scraping, large LLM calls) may be killed prematurely. Operators must increase the timeout for known slow tasks — per-definition override handles this cleanly.

### ADR-005: Subagent registry lives in `config.yaml` and enriches the system prompt

**Status:** Accepted  
**Context:** Operators need a way to declare named subagents with specific scripts, skills, and models without modifying Python. The main LLM needs to know what specialized agents are available so it can route intelligently.  
**Decision:** The `subagents.registry` block in `config.yaml` maps names to `SubagentDefinition` configs. At `AgentLoop` init, `SubagentRegistry.all_definitions()` is formatted as a markdown table and appended to the bot's system prompt. The main LLM sees this as part of its context and can reference agents by name in its tool calls. Ad-hoc spawns (by script path, no registry entry) remain supported.  
**Consequences:**
- Positive: Zero Python required to add a new named subagent; the LLM automatically learns about it at next startup; each subagent can have its own skills and model without touching the main agent's config.
- Negative: System prompt grows with each registered subagent — mitigated by keeping descriptions concise (one sentence each).
- Risk: If a registered subagent's `script` path is wrong, the error surfaces at spawn time, not at startup — mitigated by an optional `SubagentRegistry.validate()` call on startup that checks script paths exist.

---

## Architecture Diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│  monkeybot serve / monkeybot run                                       │
│                                                                        │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │  AgentLoop (loop.py)                                           │   │
│  │                                                                │   │
│  │  [init] registry = SubagentRegistry(config["subagents"])      │   │
│  │         system_prompt += registry.to_prompt_block()           │   │
│  │                                                                │   │
│  │  1. run(user_msg) → AgentEvent stream                         │   │
│  │  2. [LLM decides to call spawn_subagent tool]                 │   │
│  │       │                                                        │   │
│  │       ▼                                                        │   │
│  │  tool_args: {name: "researcher"} or {script: "path/to/x.py"} │   │
│  │       │                                                        │   │
│  │       ▼                                                        │   │
│  │  registry.resolve(name)  ──► SubagentDefinition               │   │
│  │    (or use script path directly for ad-hoc)                   │   │
│  │       │                                                        │   │
│  │       ▼                                                        │   │
│  │  spawn_subagent(definition, task, context)    ┌─────────────┐ │   │
│  │  ┌──────────────────────────────────────────► │ child script│ │   │
│  │  │  stdin:  SubagentEnvelope JSON line        │ (researcher │ │   │
│  │  │  stdout: AgentEvent JSON lines  ◄────────  │  .py or    │ │   │
│  │  │                                            │  custom)   │ │   │
│  │  │  yields: SubagentStarted                  └─────────────┘ │   │
│  │  │          AssistantDelta*                                   │   │
│  │  │          SubagentCompleted                                 │   │
│  │  │                                                            │   │
│  │  3. DurableRunStore.record_started(run_id, definition.name)  │   │
│  │  4. [after child exits] record_completed/failed()            │   │
│  │                                                               │   │
│  │  5. finally: yield TurnComplete                              │   │
│  │     → _on_turn_complete(session_id, tc)                      │   │
│  │          └─ asyncio.create_task(run_council(...))            │   │
│  └────────────────────────────────────────────────────────────┘   │
│                                                                        │
│  ┌──────────────────────┐       ┌──────────────────────────────┐   │
│  │  SubagentRegistry    │       │  Council (council.py)        │   │
│  │  (subagent_registry  │       │                              │   │
│  │   .py)               │       │  run_council(text, mem_path) │   │
│  │                      │       │  → LLM call w/ COUNCIL_PROMPT│   │
│  │  resolve(name)       │       │  → parse structured sections │   │
│  │  all_definitions()   │       │  → save_memory(key, content) │   │
│  │  to_prompt_block()   │       │  → writes ≥1 .md memory file │   │
│  │  validate()          │       └──────────────────────────────┘   │
│  └──────────────────────┘                                            │
│                                                                        │
│  ┌─────────────────────┐                                              │
│  │  DurableRunStore     │                                             │
│  │  (durable_runs.py)  │                                              │
│  │                     │  aiosqlite                                   │
│  │  record_started()   │ ──────────────────────────────────────────► │
│  │  record_completed() │                                              │
│  │  record_failed()    │       ┌────────────────────────────────┐    │
│  │  pending_runs()     │       │  SQLite DB  (monkeybot.db)     │    │
│  └─────────────────────┘       │  ┌──────────┐ ┌─────────────┐ │    │
│                                 │  │ messages │ │durable_runs │ │    │
│                                 │  ├──────────┤ ├─────────────┤ │    │
│                                 │  │turn_usage│ │run_id  (PK) │ │    │
│                                 │  ├──────────┤ │agent_name   │ │    │
│                                 │  │ job_runs │ │script       │ │    │
│                                 │  └──────────┘ │status       │ │    │
│                                 │               │scratch_dir  │ │    │
│                                 │               └─────────────┘ │    │
│                                 └────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Core Data Model

### `SubagentDefinition` dataclass

Loaded from `config.yaml` by `SubagentRegistry`. One instance per named subagent.

```
SubagentDefinition
├── name            str        — Registry key (e.g. "researcher", "reviewer")
├── script          str        — Path to the subagent Python script
├── description     str        — One sentence shown in the system prompt to the main LLM
├── skills_path     str | None — Scoped skills dir; falls back to bot's global skills_path if None
├── model           str | None — Model override; falls back to bot's default model if None
└── timeout_seconds int        — Per-agent timeout; falls back to subagents.timeout_seconds
```

### `SubagentEnvelope` dataclass

The payload written to the child's stdin. Carries the resolved definition values so the child script is self-contained.

```
SubagentEnvelope
├── run_id          str        — ULID for this subagent run
├── parent_run_id   str | None — Parent AgentLoop's run_id
├── agent_name      str | None — Registry name if spawned via registry; None for ad-hoc
├── task            str        — Natural language task description (set by the main LLM)
├── context         dict       — Arbitrary key-value context (caller can add anything)
├── skills_path     str        — Resolved skills directory (from definition or bot default)
├── model           str        — Resolved model (from definition or bot default)
└── scratch_dir     str        — Absolute path to the run's isolated working dir
```

### `durable_runs` table

Stores one row per subagent spawn. Written before the subprocess starts; updated on completion or failure.

```
durable_runs
├── run_id          TEXT    PRIMARY KEY          — ULID, matches SubagentStarted.run_id
├── parent_run_id   TEXT                         — AgentLoop's run_id; NULL for top-level
├── agent_name      TEXT                         — Registry name if known; NULL for ad-hoc
├── script          TEXT    NOT NULL             — Path of the subagent script
├── status          TEXT    NOT NULL DEFAULT 'running'
│                                                  values: running | completed | failed
├── scratch_dir     TEXT    NOT NULL             — Isolated working dir for this run
├── error_msg       TEXT                         — Non-NULL on failure
├── started_at      INTEGER NOT NULL             — Unix ms
└── completed_at    INTEGER                      — NULL until terminal state

Indexes:
  PRIMARY KEY (run_id)
  INDEX idx_durable_runs_status ON durable_runs(status)  -- for pending_runs() query
  INDEX idx_durable_runs_parent ON durable_runs(parent_run_id)
```

### Memory file naming convention (council output)

The council writes files to `{memory_path}/`. Naming follows the existing convention from `memory.py`:

```
{memory_path}/
├── {YYYY-MM-DD}-session-{session_id[:8]}.md   — session summary
└── {topic-slug}.md                             — topic-keyed learnings (upsert/append)
```

---

## `config.yaml` Schema (subagent section)

```yaml
subagents:
  timeout_seconds: 300          # global default; overridable per-agent
  registry:
    researcher:
      script: "subagents/researcher.py"
      description: "Searches the web and summarizes findings on a given topic."
      skills_path: ".agents/skills/research"   # optional; falls back to bot default
      model: "gemini-2.0-pro"                  # optional; falls back to bot default
      timeout_seconds: 120                     # optional; overrides global default

    reviewer:
      script: "subagents/reviewer.py"
      description: "Reviews a draft for clarity, accuracy, and tone."
      # skills_path and model omitted → use bot defaults

    poster:
      script: "subagents/poster.py"
      description: "Posts approved content to the configured social channels."
      timeout_seconds: 60

council:
  enabled: true
```

The `subagents.registry` block is **optional**. If omitted, the system still works — the main LLM just won't have any named agents in its prompt, and `spawn_subagent` requires an explicit script path.

---

## Key Design Decisions

1. **Two spawn paths, one function.** `spawn_subagent(definition_or_script, task, context)` accepts either a `SubagentDefinition` (registry lookup already done) or a raw script path string (ad-hoc). This keeps the call site simple while supporting both patterns.

2. **The registry enriches the system prompt, not a tool schema.** Named subagents appear as a markdown table in the system prompt ("Available Subagents: researcher — searches the web…"). The main LLM references them by name in its `spawn_subagent` tool call args. This is simpler than encoding subagent names as a dynamic enum in the tool JSON schema.

3. **Each subagent reads its resolved config from `SubagentEnvelope`** — not from a config file. This means `researcher.py` does not need to know where `config.yaml` is. It gets `skills_path`, `model`, and `scratch_dir` directly from the envelope it reads off stdin.

4. **`spawn_subagent()` is an async generator, not a coroutine.** It yields `AgentEvent` objects as the child emits them — the parent's event stream stays live and the UI sees real-time progress from the subagent.

5. **`create_scratch_dir(run_id)` uses `tempfile.mkdtemp` with a predictable prefix** (`monkeybot-run-{run_id}`). This makes logs easy to find without a DB lookup and survives process restarts (the dir is still there even if the DB says `running`).

6. **`run_council()` receives the raw conversation text** (not a list of `Message` objects) so it has zero coupling to `ConversationHistory`. The caller (the `_on_turn_complete` hook) is responsible for loading and serializing the history.

7. **The council follows a Read-Merge-Write pattern.** Before calling the LLM, `run_council()` reads all existing managed category files (`user-preferences.md`, `key-facts.md`, `open-questions.md`) and passes their current content into `COUNCIL_PROMPT`. The LLM sees both the existing memory AND the new session and produces a single complete, deduplicated output for each category. The code then overwrites each category file with the LLM's merged result. Deduplication is the LLM's responsibility, not the code's — no diffing logic required. Dated session files (`{YYYY-MM-DD}-session-*.md`) are append-only and never merged.

8. **`pending_runs()` returns `status = 'running'` rows only.** A row stays `running` if the process was killed before calling `record_completed()` or `record_failed()`. The recovery path is: `pending_runs()` → operator decides to retry or mark failed.

---

## Next Steps

- **Phase 1B:** Define `SubagentRegistry` full API (`resolve`, `all_definitions`, `to_prompt_block`, `validate`), `SubagentDefinition` field constraints, complete `SubagentEnvelope` schema, `spawn_subagent()` full signature (including timeout resolution order), `DurableRunStore` public API, `run_council()` full signature, `COUNCIL_PROMPT` template, and the full test strategy (unit + integration).
- **Phase 1C:** Error handling for unknown registry names, script-path validation at startup, scratch dir cleanup policy, council LLM provider selection, ruff/mypy compliance notes.
