# MonkeyBot Harness — Features & Design Reference

This document anchors the **MonkeyBot harness** — the runtime that owns turn semantics, tool dispatch, prompt composition, memory, and persistence. Use it when adding or modifying features so new work stays compatible with existing behavior and invariants.

**Related docs:** [Getting Started](getting-started.md) · [SSE Gateway](sse-gateway-ui.md) · [MCP](mcp.md) · [Skills](skills.md) · [Cloud deployment](cloud-deployment-design.md)

---

## Architecture overview

MonkeyBot is a thin framework for **tool-using LLM agents**. The harness owns orchestration; the gateway is transport; providers are adapters; MCP and custom tools extend capabilities at runtime.

```
Client (CLI / chat UI / serverless handler)
    → FastAPI SSE gateway (routes, session bus)
        → GatewayLoopPort.start_turn()
            → build_context() → TurnContext
            → loop.run() → Provider.stream() + tool dispatch
            → HistoryStore + UsageStore
```

### Layer responsibilities

| Layer | Location | Owns |
|-------|----------|------|
| **Gateway** | `src/monkeybot/gateway/sse/` | HTTP/SSE transport, session bus, CORS, pending UI responses |
| **Harness loop** | `src/monkeybot/core/runtime/loop.py` | Turn semantics, streaming, tool batching, hooks, summarization |
| **Context** | `src/monkeybot/core/context/` | Per-turn `TurnContext`, tool defs, curation, output budgeting |
| **Prompts** | `src/monkeybot/core/prompts/` | System prompt composition (operator + harness + volatile tail) |
| **Providers** | `src/monkeybot/providers/` | Vendor LLM adapters (Gemini, OpenAI, Claude, Bedrock, …) |
| **Tools** | `src/monkeybot/core/tools/` | Built-in tools, inspectors, sandbox routing |
| **MCP** | `src/monkeybot/core/mcp/` | MCP client, stdio/HTTP servers, runtime add/remove |
| **Memory** | `src/monkeybot/core/memory/` | Durable markdown memory, hook, organizer |
| **Persistence** | `src/monkeybot/core/persistence/` | History, usage, durable subagent runs |
| **Bootstrap** | `src/monkeybot/core/bootstrap.py` | Library/FaaS embed pattern (Pattern B/C) |

### Deployment patterns

| Pattern | Use case | Entry |
|---------|----------|-------|
| **A** | Local dev, Cloud Run, Docker | `python -m monkeybot.gateway.main` |
| **B/C** | Lambda, Cloud Functions, custom handlers | `create_harness_deps()` + `run_pattern_bc_turn()` |

### Core dependency rule

**Core must not import gateway.** Gateway imports core via ports (`LoopPort`, `PendingResponseBusPort`, `ToolExecutorPort`). New features in `core/` must stay gateway-agnostic.

---

## Turn lifecycle

One **user message** may span multiple **inner turns** (model → tools → model → …) until the model produces final assistant text or `MAX_TURNS` is reached.

### Sequence (one user message)

1. Append user message to history; fire `USER_MESSAGE` hook.
2. **Inner turn loop** (up to `MAX_TURNS`, default 50):
   - Refresh memory index; optional context curation (turn 1 only).
   - Compose system prompt; resolve attachments; preflight token count.
   - Optionally summarize history or shape tool outputs under context pressure.
   - Stream provider; accumulate tool calls until `Done`.
   - Execute tools (inspectors → hooks → executor); append assistant + tool rows.
   - Repeat until final assistant text or max turns.
3. Emit `TurnComplete` with usage totals and optional trace id.

### Inner-turn phases

| Phase | When | Key files |
|-------|------|-----------|
| Hooks (`PRE_TURN`) | Turn 1 of user message | `core/hooks/` |
| Context curation | Turn 1, if enabled + thresholds met | `core/context/curator.py` |
| System prompt build | Every inner turn | `core/prompts/prompt.py` |
| Attachment resolve | Before provider call | `core/attachments/resolve.py` |
| Preflight tokens | Before provider call | `core/runtime/context_budget.py` |
| History summarization | When tokens exceed trigger ratio | `loop.py` |
| Tool result shaping | Under context pressure tiers | `core/context/tool_shapers.py` |
| Provider stream | Every inner turn | `providers/*.py` |
| Tool execution | When model emits tool calls | `core/tools/core_tool_executor.py` |
| History append | After tool batch | `core/persistence/history.py` |

### Gateway SSE flow

1. `POST /sessions` → create session
2. `GET /sessions/{id}/events` → SSE stream
3. `POST /sessions/{id}/reply` → `start_turn()` (background)
4. Events: `Thinking`, `AssistantDelta`, `ToolCallStarted`, `ToolCallResult`, `ContextSummarizing`, `SystemPromptSnapshot`, `TurnComplete`, `Error`
5. `POST /sessions/{id}/cancel` → cooperative cancellation

---

## Feature catalog

Each section follows: **Purpose** · **Key files** · **How it works** · **Depends on** · **Invariants**

---

### 1. Owned agent loop (`loop.run`)

**Purpose:** Single source of truth for harness turn semantics.

**Key files:** `core/runtime/loop.py`, `core/runtime/events.py`

**How it works:**
- `run()` is an async generator yielding `AgentEvent` until `TurnComplete`.
- Tool calls accumulate during streaming until `Done`, then execute in **lexicographic `call_id` order**.
- Consecutive `task` tools in one batch run **in parallel** (max 10); all other tools run as **serial chunks**.
- One user `Message` per model tool-call turn — all `ToolResponse` blocks grouped together (required for Gemini replay).
- Final assistant history write is backgrounded but **awaited at turn tail** before freeze/reset.
- `repair_tool_turn_integrity()` runs on every `history.load()` (in-memory only, never persisted).

**Depends on:** Provider, HistoryStore, ToolExecutorPort, inspectors, optional hooks/curator/attachments.

**Invariants:**
- `run()` **never raises** to callers; errors become `Error` events; `TurnComplete` always emitted.
- Cooperative cancellation via `asyncio.Event`, checked at loop boundaries.
- Silent-model guard: whitespace-only assistant after tools → loop continues until budget.

---

### 2. System prompt composition

**Purpose:** Combine operator-authored base prompt with runtime-owned harness and volatile context.

**Key files:** `core/prompts/prompt.py`, `core/prompts/harness_prompt.py`, `paths.agent_md` → `AGENT.md`

**Section order (cache-friendly):**

1. **Stable prefix:** `AGENT.md` + harness + session attachments
2. **Volatile tail:** memory index + skills + "Current request" anchor

**How it works:**
- `compose_system_prompt()` builds the full system string each inner turn.
- Harness lines for `task`, `web_search`, subagent personas, and `run_command` execution mode are conditional on active tool list.
- Emission-style block (Levers 1–2: minimum code, terse prose) is injected into the stable prefix when `MONKEYBOT_EMISSION_STYLE=terse`; its dense agent-to-agent sub-block (Lever 3) is additionally gated on the `task` tool being active. Default off. See [§21](#21-emission-style-terse-output-guidance).
- `HARNESS_TOOL_CALL_PROTOCOL` enforces native tool-call channel, evidence rule, no-repeat rule.
- "Current request" block restates last user text when transcript continued with assistant/tool messages (skipped when user row is already last).
- Curated memory/skills replace ctx lists when context curation ran on turn 1.

**Depends on:** `TurnContext`, `SandboxConfig.from_env()`, attachment catalog.

**Invariants:**
- Harness text lives in **code** (`harness_prompt.py`), not `AGENT.md` — do not duplicate tool protocol in operator prompts.
- `_MAX_CURRENT_REQUEST_CHARS = 8000` caps injected user text.
- Stable prefix before volatile tail for prompt caching.
- Model should prefer **active tool list** over stale harness summaries.

---

### 3. Provider system

**Purpose:** Thin streaming boundary between harness and LLM vendors.

**Key files:**
- `core/llm/provider.py` — `Provider` protocol, `Message`, `ProviderEvent`
- `providers/gemini.py`, `openai.py`, `claude.py`, `vertex_claude.py`, `bedrock.py`, `huggingface.py`
- `core/config/settings.py` — `get_provider_config()`

**How it works:**
- `stream(messages, tools, model=..., thinking_budget=...)` yields `TextDelta`, `ThinkingDelta`, `ToolCall`, `UsageEvent`, `Done`.
- `count_input_tokens()` must match the same payload shape as `stream()` (summarization triggers, tool budgets).
- Provider resolution via `MODEL_PROVIDER` aliases (`gemini` → `google_vertexai`, `vertex-claude` → `vertex_anthropic`).
- Optional extras in `pyproject.toml`: `gemini`, `openai`, `claude`, `vertex-claude`, `bedrock`, `huggingface`.
- `MODEL_PROVIDER=fake` is gateway/test-only; unit tests inject `ScriptedFakeProvider` directly.

**Depends on:** `ToolDef`, `ContentBlock` serialization per adapter.

**Invariants:**
- Exactly one overlapping `stream()` per provider instance is undefined.
- `Message.role` is only `user` | `assistant` | `system`.
- Prompt caching: stable prefix = `AGENT.md` + harness + attachments; `MODEL_ENABLE_CACHING` toggles explicit Anthropic `cache_control`.
- Cost estimation via `providers/pricing.estimate_cost()` on usage events.

---

### 4. Tool execution (`CoreToolExecutor`)

**Purpose:** Default `ToolExecutorPort` — built-ins, MCP, custom tools, subagents.

**Key files:** `core/tools/core_tool_executor.py`, `terminal.py`, `workspace_service.py`, `sandbox_executor.py`, `spill_inventory.py`

**Built-in tools** (from `context._core_tool_defs`):

| Tool | Role |
|------|------|
| `read_file` / `write_file` | Workspace-relative paths |
| `search_memory` | Keyword search in memory tree |
| `list_skills` | Skill discovery |
| `run_command` | Allowlisted shell (host or OpenSandbox) |
| `task` | Subagent subprocess (parent only) |
| `add_mcp_server` / `remove_mcp_server` | Runtime MCP registration |
| `render_image` / `read_attachment` | When attachments enabled |

**Dispatch order:** core → `extra_tools` (e.g. `WebSearchTool`) → MCP (`server__tool` naming).

**Built-in error shape:** JSON with `ok: false`, `error_kind` (`policy` | `validation` | `runtime`), `message`, `hint`.

**Depends on:** `WorkspaceStorage`, `MCPClientPort`, optional `MemorySubsystem`, `RunStore` for task queue.

**Invariants:**
- Workspace paths: no `..`, no `~`; resolved under `workspace_root`.
- Large tool results may spill to `.monkeybot/spill/{thread_id}/`.
- `task` omitted in subagent workers (`include_task_tool=False`).
- Nested `task` disabled inside subagents.
- Custom tools must not collide with core or MCP names.

---

### 5. Tool inspectors

**Purpose:** Pre-flight policy gates; supports user confirmation via SSE.

**Key files:** `core/tools/inspector.py`

**Inspectors wired at gateway startup:**
1. `CommandTierInspector` — `command_allowlist.yaml`
2. `RulesInspector` — `MONKEYBOT_TOOL_DENIED_PATTERNS` substring deny list

**Decision kinds:** `allow` | `deny` | `confirm` (requires `ctx.sse_bus` for pending UI response).

**Invariants:**
- Missing allowlist file → all tools allowed (logged).
- Default deny patterns block package installs (`pip install`, `npm install`, etc.).

---

### 6. MCP integration

**Purpose:** Connect stdio and Streamable HTTP MCP servers; expose tools as `server__tool`.

**Key files:** `core/mcp/mcp_client.py`, `core/mcp/ports_mcp.py`, `monkeybot_config/mcp.json`

**How it works:**
- Loaded at gateway startup from `MCP_CONFIG`; `${VAR}` interpolation in config.
- `MCP_STRICT_LOAD=1` fails startup on connection errors (default: log and continue).
- Runtime `add_mcp_server` / `remove_mcp_server` mutate live connections.
- Lazy-imports `mcp` SDK to keep core importable without MCP installed.

**Invariants:**
- MCP tool names: `server__tool` (double underscore).
- MCP tool errors are plain text (not structured JSON).
- Teardown noise from AnyIO is swallowed on disconnect.

---

### 7. Tool output budgeting

**Purpose:** Prevent context blow-up from large tool results.

**Key files:** `core/context/tool_output_policy.py`, `core/context/tool_shapers.py`, `core/runtime/context_budget.py`

**How it works:**
- Context pressure tiers (`light` / `moderate` / `aggressive`) based on `used_tokens / context_window`.
- Under pressure, tool results in history are shaped/truncated.
- Per-turn tool responses are budgeted via `ContextBudgeter` before append.

**Invariants:**
- Tool result ground truth in history may be shaped for provider replay.
- Hooks inject context via system prompt, not by mutating tool results.

---

### 8. Context curation

**Purpose:** Narrow memory/skills in system prompt when catalog is large.

**Key files:** `core/context/curator.py`, `monkeybot.yaml` `context_curation:`

**How it works:**
- Runs on **turn 1 only** when `CONTEXT_CURATION_ENABLED` and thresholds met.
- Separate `curator_provider` (gateway: `GeminiProvider(thinking_budget=0, max_tokens=1024)`).
- Curated selections are **frozen for follow-up turns** in the same user message.

**Depends on:** Curator provider, memory index, skills list.

**Invariants:**
- Subagents disable curation (`enable_context_curation=False`).
- On curator failure, empty curated lists are used.

---

### 9. Conversation history

**Purpose:** Persist `Message` rows (typed `ContentBlock` JSON) per `thread_id`.

**Key files:** `core/persistence/backends.py`, `history.py`, `sqlite_backend.py`, `postgres.py`, `firestore.py`, `core/messages/tool_integrity.py`

**DB URL schemes:** `sqlite://`, `postgresql://` / `postgres://`, `firestore://PROJECT/DATABASE`

**Auto schema:** SQLite and Postgres run idempotent DDL on `open()` when `paths.auto_schema` is `true` in monkeybot.yaml (default). Set `paths.auto_schema: false` when a migration process owns the schema. Firestore is schemaless — no DDL on `open()`.

**Invariants:**
- Lazy backend imports.
- Relative `sqlite:///` paths normalized against `MONKEYBOT_AGENT_ROOT` for subagents.
- `ToolRequest`/`ToolResponse` pairing must survive provider replay.
- Integrity repair on load only — never persisted.

---

### 10. Usage accounting

**Purpose:** Per-turn token/cost rollup; exposed via `GET /sessions/{id}/usage`.

**Key files:** `core/persistence/usage.py`, `core/llm/usage.py`

**Invariants:** Recorded once per user message on `TurnComplete`.

---

### 11. Memory subsystem

**Purpose:** Durable markdown memory with automatic capture and LLM organizer.

**Key files:** `core/memory/subsystem.py`, `hook.py`, `organizer.py`, `storage_ops.py`

**Storage URI:** `local://`, `gcs://`, `s3://` via `create_workspace_storage()`

**Hook lifecycle:**

| Event | Behavior |
|-------|----------|
| `USER_MESSAGE` | Append to `chat_log.md` |
| `PRE_TURN` | Inject memory search hits into `inject_memory_lines` |
| `PRE_TOOL` | Inject file/query-specific memories into `inject_text` |
| `POST_TOOL` | Capture raw observations (skip successful read-only tools) |
| `POST_TURN` | Schedule debounced organizer |

**Invariants:**
- Write path uses `asyncio.Lock` shared with organizer.
- `MONKEYBOT_MEMORY_HOOK_ENABLED=false` disables hook registration.
- Subagents get **no-op `HookManager`** to avoid duplicate writes.
- `flush()` must be called before short-lived handlers exit if organizer work matters.

---

### 12. Session attachments

**Purpose:** User-uploaded files referenced in conversation; frozen into history.

**Key files:** `core/attachments/` (catalog, store, freeze, resolve, tools)

**Invariants:**
- Enabled via env from yaml.
- Images resolved for provider on replay.
- Catalog rebuilt from history on session resume.
- Resolve before provider; freeze before destructive history ops.

---

### 13. Subagents (`task` tool)

**Purpose:** Delegate work to isolated subprocess with same workspace/memory/MCP.

**Key files:** `core/subagents/subagent_proto.py`, `subagent_worker.py`, `monkeybot.yaml` `subagents:`

**How it works:**
- Parent passes `subagent_type` to select a named persona from `monkeybot.yaml`.
- Subagent uses separate `AGENT.md` per type.
- Returns JSON (summary, errors, usage).

**Invariants:**
- No nested `task` inside subagents.
- Max 10 parallel `task` calls per batch.
- `SUBAGENT_MAX_TURNS` / `SUBAGENT_TIMEOUT_SEC` env limits apply.

---

### 14. Durable subagent runs (task queue)

**Purpose:** Optional async subagent execution via `RunStore`.

**Key files:** `core/persistence/durable_runs.py`, `core/persistence/runs.py`, `subagents/worker/`

**How it works:**
- `MONKEYBOT_TASK_QUEUE=1` enqueues via `record_pending`; workers run `python -m monkeybot.subagents.worker`.
- Requires `DB_URL`.
- Stale claim window: `MONKEYBOT_WORKER_STALE_CLAIM_MS` (default 10 min).

**Invariants:**
- No claim heartbeat yet — long runs risk duplicate execution.
- Queue mode without storage raises at enqueue time.

---

### 15. Configuration (`monkeybot.yaml`)

**Purpose:** Declarative agent config; secrets in `.env`.

**Key files:**
- Packaged defaults: `src/monkeybot/monkeybot_config/monkeybot.example.yaml` (scaffolded to `monkeybot_config/monkeybot.example.yaml`)
- `core/config/runtime_env.py` — `ENV_MAP`, `apply_monkeybot_runtime_env()`
- `core/config/yaml_loader.py` — includes/deep-merge
- `core/config/validation.py`

**Precedence:** `.env` (dotenv) → existing `os.environ` wins → yaml fills **unset** keys only.

**Discovery:** `MONKEYBOT_CONFIG` or `<cwd>/monkeybot_config/monkeybot.yaml`.

**Major sections:** `runtime`, `paths`, `model`, `gateway`, `context_curation`, `memory_hook`, `subagent`, `subagents`, `tools`, `compression`, `web_search`, `sandbox`, `emission`, `fake_provider`, `includes`.

**Invariants:**
- Secrets never belong in yaml.
- `apply_monkeybot_runtime_env()` is idempotent.
- Subagent registry: duplicate names → `ConfigError`.
- YAML maps to env once at startup; runtime reads `os.environ`.

---

### 16. CLI

**Purpose:** Scaffold, validate, doctor, run gateway, chat REPL.

**Key files:** `cli/src/monkeybot_cli/main.py`, `commands/*.py`

| Command | Purpose |
|---------|---------|
| `monkeybot new` | Scaffold `monkeybot_config/`, workspace, `.env.example` |
| `monkeybot validate` | YAML, paths, MCP, provider env |
| `monkeybot doctor` | Python, extras, credentials, port |
| `monkeybot run` | Foreground gateway |
| `monkeybot chat` | Spawn gateway + REPL |

**Interpreter resolution:** agent `.venv` → `uv run` → CLI interpreter (legacy).

---

### 17. Hooks

**Purpose:** Lifecycle extensibility without modifying the loop.

**Key files:** `core/hooks/`

| Event | When |
|-------|------|
| `USER_MESSAGE` | User message appended |
| `PRE_TURN` | Start of inner turn 1 |
| `PRE_TOOL` | Before each tool execution |
| `POST_TOOL` | After each tool execution (fire-and-forget) |
| `POST_TURN` | End of user message turn |

**Extension API:** `HookManager.register(HookEvent, fn)` with `HookPayload.inject_text` / `inject_memory_lines`.

**Invariants:**
- Bounded timeout; errors never propagate; no re-entrancy.
- `POST_TOOL` is fire-and-forget — may not complete before `run()` returns.

---

### 18. Skills

**Purpose:** Doc-driven procedural knowledge; agent discovers via `list_skills` + `read_file`.

**Key files:** `skills/*/SKILL.md`, `docs/skills.md`

**Invariants:** Skills are filesystem docs, not auto-executed Python (legacy loader exists but primary path is doc-driven).

---

### 19. Web search

**Purpose:** Optional `web_search` custom tool.

**Key files:** `web_search/`, `web_search/build_backend`

**Backends:** `duckduckgo`, `tavily`, `firecrawl`, `none`

---

### 20. Observability

**Purpose:** OpenTelemetry spans for loop and gateway.

**Key files:** `observability/spans.py`

**Install:** `monkeybot[observability]`

---

### 21. Emission style (terse output guidance)

**Purpose:** Cut model *emission* volume (generated code and prose) — the side of the token budget that tool-output shaping (§7) does not cover. Adapted from the "honey" writing-style skill, trimmed to rules + safety carve-outs, and made always-on + cached rather than on-demand.

**Key files:** `core/prompts/harness_prompt.py` (`_EMISSION_STYLE_BLOCK`, `_EMISSION_AGENT_TO_AGENT_BLOCK`, `emission_style_terse_from_env`, `_emission_section`), `core/prompts/prompt.py`, `core/config/runtime_env.py` (`ENV_MAP`), `monkeybot.yaml` `emission:`

**How it works:**
- Opt-in per deployment: `emission.style: terse` in `monkeybot.yaml` → `MONKEYBOT_EMISSION_STYLE` env (precedence: real env / `.env` wins over yaml). Accepted values: `terse | true | 1 | on | yes` (case-insensitive). Default off.
- `compose_system_prompt()` reads `emission_style_terse_from_env()` and passes it to `harness_fixed_context(emission_style=...)`.
- Two sub-blocks, both in the **stable prefix** (before `HARNESS_TOOL_CALL_PROTOCOL`):
  - **Levers 1–2** (always when enabled): minimum code that needs to exist; terse prose (answer first, fragments, no narration of readable code); keep-exact carve-out (verbatim code, identifiers, paths, commands, versions, error messages).
  - **Lever 3** (additionally gated on `include_task_tool`): dense agent-to-agent handoffs for `task` results — minified JSON, address records by stable key, aggregate-in-code, columnar for uniform arrays.
- Safety carve-outs reconcile terseness with existing rules: never cut input validation / error handling / security; never cut the blocker report (defers to the evidence rule); never cut anything the user explicitly asked for; keep function bodies when editing code.

**Depends on:** `harness_fixed_context`, active tool list (for `task` gating), `ENV_MAP`.

**Invariants:**
- Default off — no prompt change for any deployment unless opted in (e.g. leave off for conversational/user-facing deployments like `parent_financial_coach`).
- Lives in the stable cacheable prefix; does not disturb the stable/volatile split (§2). `out.endswith(HARNESS_TOOL_CALL_PROTOCOL)` still holds.
- The agent-to-agent (Lever 3) sub-block appears **only** when the `task` tool is active.
- Does not override the evidence rule, no-repeat rule, or tool-call protocol.
- No ESO codec / `eso stash` handle system — reversible large-payload retrieval is the harness-level tool-artifact store's job (future work), not this feature.

---

## Content model

### Message

`Message` = `role` (`user` | `assistant` | `system`) + `list[ContentBlock]`

### Content blocks (closed union, `type` discriminator)

`Text`, `Thinking`, `ToolRequest`, `ToolResponse`, `Image`, `File`, …

Shared serde for SQLite persistence and provider adapters.

### ToolDef

`name`, `description`, `input_schema` (JSON Schema object)

---

## Extension points

| Extension | Mechanism | Key API |
|-----------|-----------|---------|
| Custom in-process tools | `extra_tools: Sequence[CustomTool]` in `build_context` | `CustomTool` protocol |
| MCP servers | Static `mcp.json` or runtime `add_mcp_server` | `MCPClient` |
| Hooks | `HookManager.register(HookEvent, fn)` | `HookPayload` |
| Memory | `MemorySubsystem.register_hooks()` | Automatic via hooks |
| Tool inspectors | Implement `ToolInspector.check()` | Pass list to `loop.run()` |
| Provider override | `create_harness_deps(provider_override=...)` | `Provider` protocol |
| Storage backend | `create_storage_backend(db_url)` | New URL scheme |
| Workspace storage | `create_workspace_storage(uri)` | `WorkspaceStorage` protocol |
| Web search backend | `WEB_SEARCH_BACKEND` | `web_search/build_backend` |
| Fake provider | `ScriptedFakeProvider` / `MONKEYBOT_FAKE_PROVIDER_EVENTS` | Deterministic tests |

---

## Global invariants checklist

Use this when reviewing PRs or designing new features.

| Area | Rule |
|------|------|
| **Loop** | Never raise from `run()`; always emit `TurnComplete` |
| **Tools** | Native function-call channel only; no JSON-in-prose tool calls |
| **History** | `ToolRequest`/`ToolResponse` pairing must survive provider replay |
| **Tool ordering** | Lexicographic `call_id`; one user row per tool batch |
| **Parallelism** | Consecutive `task` only (max 10); other tools serial |
| **Prompt** | Stable prefix before volatile tail for caching |
| **Harness** | Tool protocol in code, not `AGENT.md` |
| **Emission style** | Opt-in (default off); agent-to-agent sub-block gated on `task` tool; never overrides evidence/no-repeat rules |
| **Hooks** | Bounded, silent failures, no re-entrancy |
| **Memory** | Subagents must not register parent hooks |
| **Security** | `run_command` always allowlisted; path escape blocked |
| **MCP** | `server__tool` naming; double underscore |
| **Subagents** | No nested `task` |
| **Config** | Env beats yaml; secrets in `.env` only |
| **Gateway** | Core stays gateway-agnostic |
| **Providers** | `count_input_tokens` consistent with `stream` payload |
| **Attachments** | Resolve before provider; freeze before destructive history ops |
| **Cancellation** | Cooperative via `asyncio.Event` |
| **Usage** | Recorded once per user message on `TurnComplete` |
| **History write** | Final assistant row awaited at turn tail |

---

## Default limits (env-overridable)

| Setting | Default | Env / config |
|---------|---------|--------------|
| Max inner turns | 50 | `MAX_TURNS` / `model.max_turns` |
| Context window | 200000 (example yaml: 1M) | `MODEL_CONTEXT_WINDOW` |
| Summarization trigger | ratio from compression config | `SUMMARY_TRIGGER_RATIO` |
| Read max lines | 5000 | `MONKEYBOT_READ_MAX_LINES` |
| Default read lines | 2000 | built-in |
| Context curation timeout | 10s | `context_curation.timeout_sec` |
| Current request cap | 8000 chars | code constant |
| Emission style | off | `MONKEYBOT_EMISSION_STYLE` / `emission.style` |
| Pending response timeout | 300s | `gateway.pending_response_timeout_sec` |
| Subagent timeout | 600s | `subagent.timeout_sec` |
| Subagent max turns | 25 | `subagent.max_turns` |

---

## Testing guide

### Unit / integration (`tests/`)

| Area | Location |
|------|----------|
| Loop semantics | `tests/core/test_loop.py` |
| Prompt composition | `tests/core/test_prompt.py`, `tests/core/test_harness_prompt.py` |
| Tools | `tests/core/test_core_tool_executor.py` |
| Memory | `tests/core/memory/` |
| Providers | `tests/providers/` |
| Gateway | `tests/gateway/` |
| Integration | `tests/integration/` |

**Key test doubles:**
- `core/testing/mocks_provider.py` — `ScriptedFakeProvider`
- `tests/core/test_loop.py` — `FakeHistory`, `FakeProvider`, `RecordingExecutor`, `AllowInspector`

**Global fixture:** `tests/conftest.py` forces `SANDBOX_ENABLED=false`.

### Behavioral evals (`tests/evals/`)

YAML-driven scenarios asserting harness behavior without live LLM.

- `tests/evals/scenario_runner.py`
- `tests/evals/eval_hook.py`
- `tests/evals/scenarios/*.yaml`

**Note:** Assert tools via `RecordingExecutor`; `POST_TOOL` hook may not complete before `run()` returns.

### Judge-based evals (`evals/`)

Separate harness at repo root for higher-level agent evaluation (`evals/main.py`).

---

## Module quick reference

| Concern | Start here |
|---------|------------|
| Loop / turn semantics | `src/monkeybot/core/runtime/loop.py` |
| System prompt | `src/monkeybot/core/prompts/prompt.py`, `harness_prompt.py` |
| Gateway wiring | `src/monkeybot/gateway/sse/app.py` |
| Tool dispatch | `src/monkeybot/core/tools/core_tool_executor.py` |
| Config | `core/config/runtime_env.py`, `src/monkeybot/monkeybot_config/monkeybot.example.yaml` |
| MCP | `core/mcp/mcp_client.py`, `docs/mcp.md` |
| Memory | `core/memory/subsystem.py`, `core/memory/hook.py` |
| Library embed | `core/bootstrap.py` |
| CLI | `cli/src/monkeybot_cli/main.py` |

---

## Adding a new feature — workflow

1. **Read this doc** — identify which existing features your change touches.
2. **Check invariants** — especially loop, history, prompt ordering, tool dispatch.
3. **Identify extension point** — prefer hooks, `CustomTool`, inspectors, or ports over direct loop edits.
4. **Update harness prompt** if the feature adds tools or changes protocol (`harness_prompt.py`).
5. **Update `monkeybot.example.yaml`** if new config is needed (never secrets).
6. **Add tests** — unit test with doubles; eval scenario if behavior is cross-cutting.
7. **Update this doc** — add a feature section and any new invariants.
