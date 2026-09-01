# monkeybot Harness — Features & Design Reference

This document anchors the **monkeybot harness** — the runtime that owns turn semantics, tool dispatch, prompt composition, memory, and persistence. Use it when adding or modifying features so new work stays compatible with existing behavior and invariants.

**Related docs:** [Getting Started](getting-started.md) · [Local Ollama](ollama-local.md) · [SSE Gateway](sse-gateway-ui.md) · [MCP](mcp.md) · [Skills](skills.md) · [Cloud deployment](cloud-deployment-design.md)

---

## Architecture overview

monkeybot is a thin **harness** for tool-using LLM agents. It owns orchestration; the gateway is transport; providers are adapters; MCP and custom tools extend capabilities at runtime.

**Positioning:** The runtime is **multi-cloud-capable** (Postgres/SQLite/Firestore + local/GCS/S3 memory; multiple LLM adapters; Patterns A/B/C). **Docs and examples lean GCP-first** — see [Cloud deployment — Positioning](cloud-deployment-design.md#positioning) for what is shipped on AWS vs planned on Azure.

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
| **Harness loop** | `src/monkeybot/core/runtime/` (`loop.py` facade; `turn_loop.py`, `tool_dispatch.py`, helpers) | Turn semantics, streaming, tool batching, hooks, summarization |
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

### Harness ownership boundaries

Use this table before adding features so PRs do not thicken the wrong layer: say what the harness owns, what it deliberately does not, and what must live outside the process.

| Concern | Ownership | Guidance |
|---------|-----------|----------|
| Turn semantics (`loop.run`), tool batching, doom-loop, truncated-batch reject, history compaction | **In core** | Single source of truth; keep gateway-agnostic |
| System prompt composition + tool-call protocol | **In core** | Protocol lives in `harness_prompt.py`, not `AGENT.md` |
| Built-in tools, path confinement, command allowlists, tool inspectors, HITL confirm events | **In core** | Policy-rich by design |
| MCP client + `server__tool` naming | **In core** | First-class harness infrastructure |
| Subagent `task` (subprocess `loop.run`, no nested task) | **In core** | Product multi-agent via extensions/OS is out of scope here |
| History / usage / storage backends, memory hooks | **In core** | Persist via ports; backends are pluggable |
| LLM vendor adapters | **In core (thin)** | Normalize stream/count tokens only; no product policy |
| HTTP/SSE, session bus, CORS, pending UI responses | **Not in core — gateway** | Transport only; call ports into the harness |
| Chat UI, CLI chrome, deploy manifests, billing | **Not in core — product / OS** | Consume events and APIs; do not fork loop semantics |
| Operator persona, domain procedures, skill playbooks | **Not in core — `AGENT.md` / skills** | Content the harness injects; not harness code |
| Domain / third-party tools | **Extension — `CustomTool` / MCP / hooks** | Prefer these over editing `turn_loop.py` / `tool_dispatch.py` or core tool tables |
| Strong isolation for untrusted or unattended work | **OS / container / VM** | Optional OpenSandbox + allowlists are defense-in-depth, not a full trust boundary; real isolation is external |

**PR smell checks:** new HTTP concerns in `core/` → wrong layer; new turn semantics only in the gateway → wrong layer; security that assumes “sandbox = safe” without an OS boundary → document the gap instead of implying a stronger guarantee.

---

## Turn lifecycle

One **user message** may span multiple **inner turns** (model → tools → model → …) until the model produces final assistant text or `model.max_turns` is reached.

### Sequence (one user message)

1. Append user message to history; fire `USER_MESSAGE` hook.
2. **Inner turn loop** (up to `model.max_turns`, default 1000):
   - Drain **steer** queue (mid-turn user injections) into history; emit `UserSteered`.
   - Await hook **settlement** (fire-and-forget `POST_TOOL` / prior write-side hooks) with bounded timeout.
   - Refresh memory index; optional context curation (turn 1 only).
   - Compose system prompt; resolve attachments; preflight token count.
   - Optionally summarize history or shape tool outputs under context pressure.
   - Stream provider; accumulate tool calls until `Done`.
   - Execute tools (inspectors → `ToolCallStarted` → await settlement/`PRE_TOOL` → executor); append assistant + tool rows.
   - Doom-loop check: identical tool streak (ok or error) may force a no-tools recovery turn.
   - Repeat until final assistant text or max turns.
3. Fire `POST_TURN`; settle fire-and-forget hooks (bounded); emit `TurnComplete` with usage totals and optional trace id.
4. Gateway drains **follow-up** queue (if any) and starts the next turn under the same session lock.

### Input admission (steer vs follow-up)

| Mode | Endpoint | When accepted | When applied |
|------|----------|---------------|--------------|
| Reply | `POST /sessions/{id}/reply` | Session idle | Starts a turn immediately (`SESSION_BUSY` if busy) |
| Steer | `POST /sessions/{id}/steer` | Session busy | Injected after current tool batch / before next provider call |
| Follow-up | `POST /sessions/{id}/queue` | Busy → enqueue; idle → start | FIFO drain after `TurnComplete` / lock release |

Do **not** conflate with HITL `ToolConfirmationRequest`. Cancel clears pending steer; follow-ups survive. Caps: `MONKEYBOT_STEER_QUEUE_MAX` (default 8), `MONKEYBOT_FOLLOW_UP_QUEUE_MAX` (default 16).

**Process-local only:** steer/follow-up queues live on the in-process `SessionBus` (same constraint as the SSE registry). Multi-replica gateways do not share admission queues across instances — pin sticky sessions to one replica, or treat `/queue` as best-effort for single-process deployments. If drain cannot acquire the durable turn lock (another replica / stale claim), the item is requeued and retried on an interval (`MONKEYBOT_FOLLOW_UP_LOCK_RETRY_S`, default 1s) until the lock frees or the wait budget expires (`MONKEYBOT_FOLLOW_UP_LOCK_WAIT_MS`, default = session-turn stale window), after which that follow-up is dropped so the queue cannot wedge forever.

### Inner-turn phases

| Phase | When | Key files |
|-------|------|-----------|
| Steer drain | Start of each inner turn | `core/runtime/input_admission.py`, `turn_loop.py` |
| Hook settlement | Before provider call / `TurnComplete` | `core/hooks/`, `loop_hooks.py` |
| Hooks (`PRE_TURN`) | Turn 1 of user message | `core/hooks/` |
| Context curation | Turn 1, if enabled + thresholds met | `core/context/curator.py` |
| System prompt build | Every inner turn | `core/prompts/prompt.py`, `loop_messages.py` |
| Attachment resolve | Before provider call | `core/attachments/resolve.py` |
| Preflight tokens | Before provider call | `core/runtime/context_budget.py`, `loop_usage.py` |
| History summarization | When tokens exceed trigger ratio | `history_compaction.py`, `turn_loop.py` |
| Tool result shaping | Under context pressure tiers | `core/context/tool_shapers.py` |
| Provider stream | Every inner turn | `providers/*.py`, `turn_loop.py` |
| Tool execution | When model emits tool calls | `tool_dispatch.py`, `core/tools/core_tool_executor.py` |
| History append | After tool batch | `core/persistence/history.py`, `tool_dispatch.py` |

### Gateway SSE flow

1. `POST /sessions` → create session
2. `GET /sessions/{id}/events` → SSE stream
3. `POST /sessions/{id}/reply` → `start_turn()` (background)
4. Events: `Thinking`, `AssistantDelta`, `AssistantTextStarted`, `AssistantTextEnded`, `ToolCallStarted`, `ToolCallResult`, `ToolInputDelta`, `ThinkingBlockStarted`, `UserSteered`, `QueuedInputAccepted`, `ContextEpochStarted`, `SystemContextUpdated`, `ContextSummarizing`, `SystemPromptSnapshot`, `TurnComplete`, `Error`
5. `POST /sessions/{id}/steer` / `queue` → mid-turn inject / idle FIFO (see above)
6. `POST /sessions/{id}/cancel` → cooperative cancellation (+ clear steer)

---

## Feature catalog

Each section follows: **Purpose** · **Key files** · **How it works** · **Depends on** · **Invariants**

---

### 1. Owned agent loop (`loop.run`)

**Purpose:** Single source of truth for harness turn semantics.

**Key files:** `core/runtime/loop.py` (facade `run`), `turn_loop.py` (orchestration), `tool_dispatch.py` (tool batches), `doom_loop.py`, `tool_batch.py`, `history_compaction.py`, `events.py`

**How it works:**
- `run()` is an async generator yielding `AgentEvent` until `TurnComplete`.
- Tool calls accumulate during streaming until `Done`, then execute in **lexicographic `call_id` order**.
- Consecutive `task` tools and consecutive `parallel_safe` tools in one batch run **in parallel** (capped; default 10); mutating / unmarked tools run as **serial chunks**.
- One user `Message` per model tool-call turn — all `ToolResponse` blocks grouped together (required for Gemini replay).
- Final assistant history write is backgrounded but **awaited at turn tail** before freeze/reset.
- `transform_context()` (tool-integrity repair + UI-block strip) runs on every `history.load()` (in-memory only, never persisted); `convert_to_provider()` resolves attachments / pressure-shapes for the provider view.

**Depends on:** Provider, HistoryStore, ToolExecutorPort, inspectors, optional hooks/curator/attachments.

**Invariants:**
- `run()` **never raises** to callers; errors become `Error` events; `TurnComplete` always emitted.
- Cooperative cancellation via `asyncio.Event`, checked at loop boundaries.
- Silent-model guard: whitespace-only / empty assistant after tools → inject a recovery note and keep retrying (logged, not surfaced to the caller) until turn budget. Empty completion with no prior tools (including thinking-only first turns) → up to 2 recovery re-calls with the same note; if still empty after that, emit an exhausted `Error` and end the turn. Retries themselves never yield `Error` — only the final give-up does.
- **Doom-loop guard:** `DOOM_LOOP_THRESHOLD` consecutive identical tool calls (same name + args) within a user message — whether they succeed or fail — emit an `Error`, inject a harness system note, and force the next provider call with an empty tool list (`toolChoice`-none equivalent) so the model must reply in text. Default threshold `3`; set `0` to disable. Applies to the text agent loop only (not realtime); after each recovery turn the guard re-arms for later streaks in the same message. This catches both repeated failures and successful no-progress loops (e.g. the same screenshot call over and over). Tools marked `ToolDef.doom_loop_exempt=True` (currently `loop_status`) are skipped so identical-args polling does not force a recovery turn.
- **Truncated tool batch:** When `Done.truncated` is true (provider length/max-tokens stop) **or** every tool call in the batch has `parse_error`, the harness fails the whole batch with tool error results and does **not** execute any call — even if some args parsed as JSON (they may still be silently incomplete). Realtime has no vendor length-limit signal today (Gemini Live), so it only applies the all-`parse_error` reject path.
- **History summarization:** When preflight tokens exceed the trigger ratio and history is long enough, the middle of the transcript is compressed via a dedicated summarizer call into one assistant row prefixed `[Context Summary]:`. The summarizer is instructed to emit a fixed Markdown template (Objective, Important Details, Work State with Completed/Active/Blocked, Next Move, Relevant Files) — not freeform prose. Head/tail messages are kept; the summary replaces the middle.
- **Steer / follow-up:** Mid-turn steer injects at safe boundaries only; follow-up FIFO drains only when idle. One reply-in-flight lock still applies to `/reply`.
- **Settlement barrier:** `PRE_TOOL` is awaited before execute; fire-and-forget hooks are drained (bounded by `MONKEYBOT_HOOK_SETTLEMENT_TIMEOUT_S`, default 2s) before the next provider call and before `TurnComplete` (`run()` finally). Settlement does **not** wait on SSE client ACKs (avoids deadlock).
- **Parallel-safe tools:** `ToolDef.parallel_safe=True` for read-only core tools (`read_file`, `glob`, `grep`, `search`, `search_memory`, `list_skills`, `loop_status`, `load_file`). Mutating tools (`write_file`, `replace_in_file`, `apply_patch`, …) stay serial. MCP tools default serial. Results always append in `call_id` order.
- **Context Epoch:** At each safe provider-turn boundary the harness reconciles stable vs volatile system-context sources. The leading system message keeps an immutable epoch baseline (cache prefix). Volatile changes emit a chronological mid-conversation update (user-role, not persisted) and a `SystemContextUpdated` event. Compaction / stable-source change opens a new epoch (`ContextEpochStarted`).
- **Message pipeline:** `history → transform_context() → convert_to_provider() → Provider.stream`. Transform repairs tool integrity and strips UI-only blocks; convert resolves attachments and applies pressure shaping without mutating persisted history.
- **Streaming grammar (additive):** `AssistantTextStarted` / `AssistantTextEnded`, `ThinkingBlockStarted`, and `ToolInputDelta` supplement existing `AssistantDelta` / `ThinkingBlockDelta` / `ToolCallStarted`. Clients may ignore the new events.

---

### 2. System prompt composition

**Purpose:** Combine operator-authored base prompt with runtime-owned harness and volatile context.

**Key files:** `core/prompts/prompt.py`, `core/prompts/harness_prompt.py`, `core/context/epoch.py`, `paths.agent_md` → `AGENT.md`

**Section order (cache-friendly):**

1. **Stable prefix (epoch baseline):** `AGENT.md` + harness + session attachments
2. **Volatile tail:** current date (`YYYY-MM-DD`) + memory index + skills + "Current request" anchor
3. **Mid-conversation updates (within epoch):** chronological user message with `## System context update` when volatile sources change; leading baseline stays byte-identical for prompt cache

**How it works:**
- `compose_stable_baseline()` / `compose_volatile_tail()` split the prompt; `compose_system_prompt()` remains the full-string helper for tests/tools.
- `ContextEpochTracker.reconcile()` admits sources at each provider-turn boundary (after steer drain / settlement).
- Harness lines for `task`, `web_search`, subagent personas, and `run_command` execution mode are conditional on active tool list.
- Emission-style block (Levers 1–2: minimum code, terse prose) is injected into the stable prefix when `MONKEYBOT_EMISSION_STYLE=terse`; its dense agent-to-agent sub-block (Lever 3) is additionally gated on the `task` tool being active. Default off. See [§21](#21-emission-style-terse-output-guidance).
- `HARNESS_TOOL_CALL_PROTOCOL` enforces native tool-call channel, evidence rule, no-repeat rule.
- "Current date" injects the host-local calendar day as machine-stable `YYYY-MM-DD` in the volatile tail (not the stable cache prefix), so a midnight rollover mid-session emits a mid-conversation update without busting prompt cache.
- "Current request" block restates last user text when transcript continued with assistant/tool messages (skipped when user row is already last).
- Memory selection (`MemoryPromptSelection`) replaces full `ctx.memory_index` when truncated; skill names always come from `ctx.skills` (use `list_skills`/`read_file` for the skills root path and full `SKILL.md` procedure).

**Depends on:** `TurnContext`, `SandboxConfig.from_env()`, attachment catalog.

**Invariants:**
- Harness text lives in **code** (`harness_prompt.py`), not `AGENT.md` — do not duplicate tool protocol in operator prompts.
- `_MAX_CURRENT_REQUEST_CHARS = 8000` caps injected user text.
- Stable prefix before volatile tail for prompt caching; epoch baseline is immutable until compaction or stable-source change.
- Mid-conversation system updates are provider-view only (not written to history).
- Model should prefer **active tool list** over stale harness summaries.

---

### 3. Provider system

**Purpose:** Thin streaming boundary between harness and LLM vendors.

**Key files:**
- `core/llm/provider.py` — `Provider` protocol, `Message`, `ProviderEvent`, `ProviderCallHints`
- `providers/gemini.py`, `openai.py`, `claude.py`, `vertex_claude.py`, `bedrock.py`, `huggingface.py`, `ollama.py`, `nvidia.py`
- `core/config/settings.py` — `get_provider_config()`

**Provider catalog** (canonical ids → protocol family):

| Id | Aliases | Protocol | Cache hints |
|----|---------|----------|-------------|
| `google_vertexai` | gemini, vertex | google-genai | implicit prefix |
| `google_genai` | — | google-genai | implicit prefix |
| `openai` | — | openai-chat | session affinity + `prompt_cache_retention` when long |
| `anthropic` | — | anthropic-messages | `cache_control` + `x-session-affinity` |
| `vertex_anthropic` | vertex-claude | anthropic-messages | `cache_control` |
| `aws_bedrock` | — | anthropic-messages | `cache_control` |
| `huggingface` / `nvidia` / `ollama` | — | openai-compat | none (legacy `ollama` auto-route: local gets keep_alive) |
| `ollama-cloud` | ollama_cloud | openai-compat | none |
| `ollama-local` | ollama_local | openai-compat | `keep_alive` (default 24h) + optional `num_ctx`; no Anthropic `cache_control` |
| `fake` | — | fake | gateway/test only |

**Auth:** see CLI `monkeybot doctor` and `cli/.../providers.py` (`PROVIDER_SPECS`).

**How it works:**
- `stream(messages, tools, model=..., thinking_budget=..., hints=...)` yields `TextDelta`, optional `ToolInputDelta`, `ThinkingDelta`, `ToolCall`, `UsageEvent`, `Done`.
- Loop synthesizes `AssistantTextStarted`/`AssistantTextEnded`/`ThinkingBlockStarted` from deltas via `ProviderStreamMapper`; Anthropic streams `ToolInputDelta` from `input_json_delta`. `AssistantTextEnded.text` carries the full settled block for durable replay.
- `Done.truncated` is set when the vendor reports an output length limit (OpenAI `finish_reason=length`, Anthropic `stop_reason=max_tokens`, Gemini `MAX_TOKENS`). The text loop treats that as an unsafe tool batch. Gemini Live does not expose an equivalent signal, so realtime rejects incomplete tool batches via all-`parse_error` only.
- `count_input_tokens()` must match the same payload shape as `stream()` (summarization triggers, tool budgets).
- Provider resolution via `model.provider` aliases (`gemini` → `google_vertexai`, `vertex-claude` → `vertex_anthropic`).
- Optional extras in `pyproject.toml`: `gemini`, `openai`, `claude`, `vertex-claude`, `bedrock`, `huggingface`, `ollama`, `nvidia`.
- `ollama`, `huggingface`, and `nvidia` share the OpenAI-compatible streaming core (`providers/_openai_compat.py`) and only differ in base URL / auth. Prefer explicit ids: `ollama-cloud` always hits `https://ollama.com` and requires `OLLAMA_API_KEY`; `ollama-local` always uses the local/self-hosted host (`OLLAMA_BASE_URL`, default `http://localhost:11434`) and needs no API key. Local requests send `keep_alive` (default 24h, `model.keep_alive`) and optional pinned `num_ctx` (`model.num_ctx`) via `extra_body` so Ollama's in-memory KV prefix cache survives idle — see [Local Ollama prefix cache](ollama-local.md). Both knobs are YAML-only. Legacy `ollama` still auto-routes (a key with no URL means cloud; an explicit URL always wins). `nvidia` hits `https://integrate.api.nvidia.com/v1` and needs a free `NVIDIA_API_KEY` from build.nvidia.com.
- `model.provider: fake` is gateway/test-only; unit tests inject `ScriptedFakeProvider` directly.
- An `Image`/`File` block inside a `ToolResponse` reaches Anthropic-family, Bedrock, and Gemini models in place. The OpenAI-compat family instead promotes an `Image` into a synthetic user turn appended after the tool row (`providers/_openai_compat.py::messages_to_openai`) — `File` (PDF) results are never promoted, since Chat Completions has no document wire type.

**Prompt-cache session hints (`ProviderCallHints`):**
- Loop passes `session_id=thread_id` and `cache_retention` from `model.cache_retention` (`none` | `short` | `long`, default `short`).
- Anthropic family: `cache_control` on stable system prefix + last tool when retention ≠ `none`; optional `x-session-affinity` header.
- OpenAI: `x-session-affinity` when retention ≠ `none`; `prompt_cache_retention=24h` when `long`.
- Gemini: relies on implicit prefix caching (epoch keeps stable baseline byte-identical); hints accepted as no-ops.
- Ollama local: `keep_alive` + optional `num_ctx` on `/v1` `extra_body` (not Anthropic `cache_control`). `model.context_window` is never mapped to `num_ctx`.

**Depends on:** `ToolDef`, `ContentBlock` serialization per adapter.

**Invariants:**
- Exactly one overlapping `stream()` per provider instance is undefined.
- `Message.role` is only `user` | `assistant` | `system`.
- Prompt caching: stable prefix = `AGENT.md` + harness + attachments; Anthropic providers use explicit `cache_control` on the stable prefix when retention is enabled. Epoch keeps that prefix byte-identical across volatile-only updates.
- Cost estimation via `providers/pricing.estimate_cost()` on usage events.
- Hints are optional and provider-specific; unknown providers ignore them.

---

### 4. Tool execution (`CoreToolExecutor`)

**Purpose:** Default `ToolExecutorPort` — built-ins, MCP, custom tools, subagents.

**Key files:** `core/tools/core_tool_executor.py`, `terminal.py`, `workspace_service.py`, `patch.py`, `sandbox_executor.py`, `spill_inventory.py`

**Built-in tools** (from `context._core_tool_defs`):

| Tool | Role |
|------|------|
| `read_file` / `write_file` | `skills/...` read-only or workspace-relative paths |
| `replace_in_file` | Unique (or `replace_all`) substring edit; exact then light fuzzy match |
| `glob` / `grep` | Path discovery / content regex search (prefer over shell) |
| `apply_patch` | Multi-file Codex-style Add/Update/Delete/Move; fail-closed before any write |
| `search_memory` | Keyword search in memory tree |
| `search` / `recall` | Local knowledge index (workspace + notes); **parallel-safe**. Gateway owns writes; subagents open the index **read-only**. Harness-as-library (Pattern B/C) callers can opt into read-only via `MONKEYBOT_KNOWLEDGE_READ_ONLY=1`; the gateway always ignores this flag and stays the writer |
| `list_skills` | Skill discovery |
| `run_command` | Allowlisted shell (host or OpenSandbox) |
| `task` | Subagent subprocess (parent only) |
| `enable_mcp` / `disable_mcp` | Catalog connect / disconnect (success includes status) |
| `enable_loops` | Progressive advertise scheduled-loop tools |
| `start_loop` / `loop_status` / `pause_loop` / `resume_loop` / `stop_loop` / `disable_loops` | Scheduled loops (progressive — appear only after `enable_loops`) |
| `list_mcp_resources` / `read_mcp_resource` | MCP resources (progressive — only while an MCP server is connected) |
| `list_mcp_prompts` / `get_mcp_prompt` | MCP prompt templates (progressive — only while an MCP server is connected) |
| `load_file` | When attachments enabled (images/PDF into model context) |

**Dispatch order:** core → `extra_tools` (e.g. `WebSearchTool`) → MCP (`server__tool` naming).

**Built-in error shape:** JSON with `ok: false`, `error_kind` (`policy` | `validation` | `runtime`), `message`, `hint`.

**Depends on:** `WorkspaceStorage`, `MCPClientPort`, optional `MemorySubsystem`, `RunStore` for task queue.

**Invariants:**
- `skills/...` paths resolve only below the read-only skills root; all other relative paths resolve only below `workspace_root`.
- Writes, edits, and patches reject `skills/...`; real-path checks reject symlink escapes from either root.
- `apply_patch` validates all hunks before writing; a mid-apply failure rolls back completed ops in reverse order.
- Soft spill: large tool results always write the full payload to `.monkeybot/spill/{session_id}/` (and `.monkeybot/spill/subagent:{session_id}:*/` for task subagents) and keep a window-derived inline body in history when headroom allows; cleaned concurrently on session end. Budgets derive from `model.context_window` (no spill YAML/env knobs). Reported `read_file` line metadata is always truthful (`next_offset` when truncated).
- `task` omitted in subagent workers (`include_task_tool=False`).
- Nested `task` disabled inside subagents.
- Custom tools must not collide with core or MCP names.
- `MONKEYBOT_KNOWLEDGE_READ_ONLY` (default off) opens the knowledge index read-only for `create_harness_deps` (Pattern B/C) callers; subagent workers are always read-only regardless of the flag. The gateway SSE app is the sole writer per workspace and ignores this flag, logging a warning if it is set.

---

### 5. Tool inspectors

**Purpose:** Pre-flight policy gates; supports user confirmation via SSE.

**Key files:** `core/tools/inspector.py`, `core/tools/permission.py`, `core/tools/loop_inspector.py`

**Inspectors wired at gateway startup (order matters — first deny/confirm wins):**
1. `CommandTierInspector` — `command_allowlist.yaml` deny-regex preflight; execution allowlists stay on the executor
2. `RulesInspector` — `MONKEYBOT_TOOL_DENIED_PATTERNS` substring deny list (backward compat)
3. `PermissionInspector` — `permissions.yaml` last-match-wins `allow` / `ask` / `deny` ruleset (`PERMISSION_CONFIG`)
4. `LoopStartInspector` — `start_loop` always asks for confirmation (rich plan preview)

**Permission ruleset (`permissions.yaml`):**
- Rules are ordered; **last match wins** (OpenCode-style).
- `tool` and `pattern` support `fnmatch` wildcards (`*`, `?`).
- Resource string: normalized `run_command` line, `path` arg, or `str(args)` fallback.
- `default:` applies when nothing matches (shipped default: `allow`).
- Session approvals: POST tool-confirmation with `{approved: true, always: true}` remembers tool+resource for the rest of the session (`SessionBus.session_approvals`).

**Decision kinds:** `allow` | `deny` | `confirm` (requires `ctx.sse_bus` for pending UI response).

**Hard constraints underneath soft asks:** `allowed_commands` / `allowed_path_prefixes` + sandbox still enforce at execution time — permission `ask`/`allow` cannot bypass them.

**Invariants:**
- Missing allowlist file → tier inspector skipped (logged); executor falls back to code defaults.
- Missing `permissions.yaml` → soft ruleset disabled (logged).
- Default deny patterns block package installs (`pip install`, `npm install`, etc.).
- Confirm without `sse_bus` → deny.

---

### 6. MCP integration

**Purpose:** Connect stdio and Streamable HTTP MCP servers; expose tools as `server__tool`.

**Key files:** `core/mcp/mcp_client.py`, `core/mcp/ports_mcp.py`, `monkeybot_config/mcp.json`

**How it works:**
- Loaded at gateway startup from `MCP_CONFIG`; `${VAR}` interpolation in config.
- Listed servers are **catalogued** by default (not connected); activate with `enable_mcp` / drop with `disable_mcp`. Mid-turn tool refresh applies in the text loop.
- `"enabled": false` excludes a server from the catalog (not model-connectable). `"autoConnect": true` restores eager startup connect for that server.
- `MCP_STRICT_LOAD=1` fails startup on connection errors (default: log and continue).
- **Resources / prompts:** `list_mcp_resources` / `read_mcp_resource` and `list_mcp_prompts` / `get_mcp_prompt` appear only while at least one MCP server is connected (after `enable_mcp`, dropped on last `disable_mcp`).
- **Status:** folded into `enable_mcp` — success returns connection status + tools; failure returns the error (no separate `mcp_status` tool).
- Lazy-imports `mcp` SDK to keep core importable without MCP installed.
- Realtime sessions: MCP registry mutations refresh harness `ctx.tools`, but vendor tool schemas update only after starting a new session (v1 has no reconnect/resume).

**Invariants:**
- MCP tool names: `server__tool` (double underscore).
- MCP tool errors are plain text (not structured JSON).
- Resource/prompt meta-tools return structured JSON (`ok`, lists/payloads); connect/disconnect tools keep the same shape.
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

**Purpose:** Bound memory-index prompt size via sliding window, optional LLM curator, and search nudges.

**Key files:** `core/context/curator.py`, `core/memory/index_format.py`, `core/memory/organizer.py`, `monkeybot.yaml` `context_curation:`

**How it works:**
- **Organizer** appends INDEX.md entries in recency order and archives overflow to `INDEX.archive.md` (`memory_index_cap`, default 200).
- **Default path:** recent `memory_window_lines` (default 12). LLM curator runs only when the full index is token-heavy (`memory_token_threshold`).
- Runs when `CONTEXT_CURATION_ENABLED` and the index exceeds the window (by line count) or the token threshold.
- **Coverage/confidence** are structural (`injected/total`); when truncated, prompt nudges `search_memory`.
- Curator uses numbered `memory_line_indices`; fails open to the recency window.
- Curator skipped when index fingerprint unchanged for the thread (cache).

**Depends on:** Curator provider (token-heavy path), memory index.

**Invariants:**
- Subagents disable curation (`enable_context_curation=False`).
- Skill names are always injected in full from `ctx.skills` (use `list_skills` for the skills root, `read_file` for full `SKILL.md` procedure).

---

### 9. Conversation history

**Purpose:** Persist `Message` rows (typed `ContentBlock` JSON) per `thread_id`.

**Key files:** `core/persistence/backends.py`, `history.py`, `sqlite_backend.py`, `postgres.py`, `firestore.py`, `core/messages/tool_integrity.py`, `core/messages/transform_context.py`, `core/messages/convert_provider.py`, `core/persistence/transcript.py`, `core/runtime/events.py`

**DB URL schemes:** `sqlite://`, `postgresql://` / `postgres://`, `firestore://PROJECT/DATABASE`

**Auto schema:** SQLite and Postgres run idempotent DDL on `open()` when `paths.auto_schema` is `true` in monkeybot.yaml (default). Set `paths.auto_schema: false` when a migration process owns the schema. Firestore is schemaless — no DDL on `open()`.

**Durable vs live events (OpenCode V2-aligned):**
- **Conversation durability** lives in history (`Message` / `ContentBlock`), not an event ledger.
- **AgentEvent taxonomy:** `events.DURABLE_EVENT_KINDS` + `is_durable_event()` (non-members are live-only).
  - Durable boundaries: `ToolCallStarted`, `ToolCallResult`, `TurnComplete`, `Error`, `ContextSummarized`, `AssistantTextEnded` (with `text`), `ThinkingBlockComplete`, epoch/steer admissions, etc.
  - Live-only: streaming deltas (`AssistantDelta`, `ToolInputDelta`, `ThinkingBlockDelta`, …), progress heartbeats, playground snapshots.
- **Tool settlement:** live `ToolCallResult` mirrors durable `ToolResponse` blocks appended after the tool batch (one user row per model tool-call turn, call-order preserved). Crash between assistant `ToolRequest` append and batched responses is repaired in-memory on load (`tool_integrity`).
- **Optional NDJSON transcript** (`runtime.transcript_enabled` in monkeybot.yaml, default off, YAML-only): writes durable events plus provider request/response records into `transcript.ndjson`. Repeated tool schemas and `toolResponse` bodies are stubbed by reference (`schema_seq` / `result_seq`). The scaffold example yaml enables it so `/export-trace` works without extra setup.

**Invariants:**
- Lazy backend imports.
- All relative storage and config paths resolve once against the agent root, including subagents.
- `ToolRequest`/`ToolResponse` pairing must survive provider replay.
- Integrity repair on load only — never persisted.

---

### 10. Usage accounting

**Purpose:** Per-turn token/cost rollup; exposed via `GET /sessions/{id}/usage`.

**Key files:** `core/persistence/usage.py`, `core/llm/usage.py`

**Invariants:** Recorded once per user message on `TurnComplete`.

---

### 11. Memory subsystem

**Purpose:** Per-agent MemPalace drawers with durable outbox ingest (SQLite, Postgres, or Firestore) and wake-up + L2 recall in the prompt.

**Key files:** `core/memory/subsystem.py`, `hook.py`, `outbox.py`, `palace.py`, `writer.py`, `ingest.py`, `core/persistence/{sqlite,postgres,firestore}.py`, `core/tools/fs_isolation.py`

**Storage URI:** `local://` only (object-store palaces are not supported in this release).

**Hook lifecycle:**

| Event | Behavior |
|-------|----------|
| History append | Enqueue user / final assistant text on the outbox (same transaction when the backend supports it) |
| `PRE_TURN` | Inject thread-scoped L2 recall into `inject_memory_lines` |
| `POST_TURN` | Wake the per-agent writer |
| `SESSION_END` | Bounded outbox drain |

**Invariants:**
- Chat history is canonical; MemPalace drawers are an idempotent projection.
- Recall is scoped to the current `thread_id` by default.
- `memory.enabled: false` (or `MONKEYBOT_MEMORY_HOOK_ENABLED=0`) skips capture, wake-up, and prompt teaching. Host `run_command` children then cannot see palace files: Linux user+mount namespaces or macOS `sandbox-exec` hide those directories. If isolation cannot be established and at least one hidden palace path exists, the command is refused rather than run with palace files visible. If none of the hidden paths exist on disk, the command runs unwrapped (memory was never configured; nothing to hide). OpenSandbox does not mount the palace. The kill switch is not an argv denylist; `bash` remains allowed, and the OS hides the files.
- Postgres/Firestore persist the memory outbox with replica `palace_id` claim partitioning. Replicated deployments must share a lock-capable palace volume.
- MemPalace (chromadb / onnxruntime) is the optional `monkeybot[memory]` extra. Missing the extra disables memory instead of failing startup.
- Subagents can read the palace but do not register duplicate automatic-ingest hooks.
- `drain_writer()` runs at the end of Pattern B turns and again on `close()`, so short-lived / Lambda handlers flush the outbox before the process freezes.

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

**Key files:** `core/subagents/subagent_proto.py`, `subagent_worker.py`, `monkeybot.yaml` `subagents.personas`

**How it works:**
- Parent passes `subagent_type` to select a named persona from `monkeybot.yaml`.
- Subagent uses separate `AGENT.md` per type.
- Returns JSON (summary, errors, usage).

**Invariants:**
- No nested `task` inside subagents.
- Max 10 parallel `task` calls per batch.
- `subagents.max_turns` / `subagents.timeout_sec` YAML limits apply.
- Parent reads child NDJSON with a raised StreamReader limit (`SUBAGENT_STDOUT_LINE_LIMIT`, 16 MiB) — not asyncio’s default 64 KiB — so large `SystemPromptSnapshot` lines do not fail with `Separator is not found, and chunk exceed the limit`.
- Subagent workers redact `SystemPromptSnapshot.text` on the NDJSON pipe (parent drain ignores it; full index would otherwise inflate every inner-turn line).

---

### 14. Durable subagent runs (task queue)

**Purpose:** Optional async subagent execution via `RunStore`.

**Key files:** `core/persistence/durable_runs.py`, `core/persistence/runs.py`, `subagents/worker/`

**How it works:**
- `MONKEYBOT_TASK_QUEUE=1` enqueues via `record_pending`; workers run `python -m monkeybot.subagents.worker`.
- Requires `DB_URL` / storage — queue mode without storage raises at enqueue time.
- Production: standalone `python -m monkeybot.subagents.worker`. Development only: `MONKEYBOT_WORKER_POOL=1` on the gateway (same event loop as SSE — do not use in production). See [Cloud deployment](cloud-deployment-design.md) for multi-process notes.

**Worker tuning:**

| Variable | Default | Purpose |
|---|---|---|
| `MONKEYBOT_WORKER_STALE_CLAIM_MS` | `600000` (10 min) | Reclaim `running` rows with no heartbeat after this window; another worker may re-execute the run |
| `MONKEYBOT_WORKER_POLL_INTERVAL_S` | `2` | Poll interval for `pending_runs()` |
| `MONKEYBOT_WORKER_CONCURRENCY` | `1` | Max concurrent claimed runs per worker |
| `MONKEYBOT_WORKER_ID` | auto | Worker identity for claim attribution |

**Invariants:**
- No claim heartbeat yet — subagent runs longer than `MONKEYBOT_WORKER_STALE_CLAIM_MS` risk duplicate execution. Increase the limit for long LLM workloads or keep runs under the window.
- Queue mode without storage raises at enqueue time.

---

### 15. Configuration (`monkeybot.yaml`)

**Purpose:** Declarative agent config; secrets in `.env`.

**Key files:**
- Packaged defaults: `cli/src/monkeybot_cli/scaffold_defaults/monkeybot.example.yaml` (scaffolded to `monkeybot_config/monkeybot.example.yaml`)
- `core/config/runtime_env.py` — `ENV_MAP`, `apply_monkeybot_runtime_env()`
- `core/config/yaml_loader.py` — includes/deep-merge
- `core/config/validation.py`

**Precedence:** `.env` (dotenv) → existing `os.environ` wins → yaml fills **unset** keys only.

**Discovery:** `MONKEYBOT_CONFIG`, otherwise the nearest ancestor of the launch
directory that contains `monkeybot_config/`. The discovered agent root loads
its `.env` before YAML values fill still-unset environment variables.

**Major sections:** `runtime`, `paths`, `model`, `gateway`, `context_curation`, `memory`, `subagents`, `tools`, `compression`, `web_search`, `sandbox`, `emission`, `fake_provider`, `includes`.

**Invariants:**
- Secrets never belong in yaml.
- `apply_monkeybot_runtime_env()` is idempotent.
- Subagent registry: duplicate names → `ConfigError`.
- YAML maps to env once at startup; runtime reads `os.environ`.
- Every relative YAML path is anchored at the agent root, not the process working directory.

---

### 16. CLI

**Purpose:** Scaffold, validate, doctor, run gateway, chat REPL.

**Key files:** `cli/src/monkeybot_cli/main.py`, `commands/*.py`

| Command | Purpose |
|---------|---------|
| `monkeybot new` | Scaffold `monkeybot_config/`, workspace, `.env.example` |
| `monkeybot validate` | YAML, paths, MCP, provider env |
| `monkeybot doctor` | Python, harness, extras, credentials, port |
| `monkeybot run` | Foreground gateway (fail-closed harness probe) |
| `monkeybot chat` | Spawn SSE gateway + REPL |
| `monkeybot talk` | Realtime WebSocket client (audio/text) |

**Chat TUI (Textual):** Claude-Code-style interaction on top of the slash palette and history
search — `Esc` interrupts the active turn (double-tap while idle to recall your last message),
`Shift+Tab` cycles an approval mode (`normal` / `auto-approve` / `deny-confirms`, client-side only —
it auto-answers tool *confirmation* prompts, elicitations still ask), `@` fuzzy-inserts a workspace
file path, `!<command>` runs a local shell command shown in the transcript but never sent to the
agent, and `?` opens a shortcuts overlay. Slash commands added: `/clear`, `/model` (starts a new
session — no mid-session model swap exists), `/status`, `/config`. See
[getting-started.md](getting-started.md#chat-tui-shortcuts) for the full key table.

**Agent-first dependencies:** The CLI is thin — provider/storage extras (`bedrock`, `postgres`, …) are declared on the **agent** `pyproject.toml`, not the global CLI. `monkeybot run` / `chat` / `talk` / `doctor` resolve the interpreter as:

1. `<agent>/.venv/bin/python` when a project venv exists
2. `uv run python -m monkeybot.gateway.main` when `<agent>/pyproject.toml` exists but no `.venv`
3. `sys.executable` (CLI interpreter) — config-only trees, when that interpreter already has MonkeyBot 3.x (and MemPalace if memory is on)
4. A CLI-managed cache venv (`~/.cache/monkeybot/runtimes/…`) — config-only trees with memory on when the CLI interpreter cannot import MemPalace. Pinned to `monkeybot[memory]==<running version>`; never rewrites an agent `pyproject.toml`.

Before spawning the gateway, the CLI probes that interpreter for MonkeyBot `>=3.0.0,<4` and, when memory is enabled, MemPalace. A stale agent venv may be refreshed with `uv sync` against the existing lock; `pyproject.toml` pins are never rewritten. A failed probe prints an upgrade command and exits. `monkeybot doctor` reports the same check as `env.harness.compatible`.

`monkeybot doctor` remediation for provider extras points at adding `monkeybot[<extra>]` to the agent project, then `uv sync`.

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
| `TOOL_DEFINITION` (`tool.definition`) | Before each provider call; may filter/replace `payload.tools` |
| `BEFORE_PROVIDER_REQUEST` | After messages are finalized; may rewrite `provider_messages` / `tools` |
| `AFTER_PROVIDER_RESPONSE` | After stream success or failure (fire-and-forget); observational |

**Extension API:** `HookManager.register(HookEvent, fn)` with `HookPayload.inject_text` / `inject_memory_lines`. Provider hooks also use `tools`, `provider_messages`, `assistant_text`, `usage`, `provider_error`.

**Invariants:**
- Bounded timeout; errors never propagate; no re-entrancy.
- `POST_TOOL` / `POST_TURN` / `AFTER_PROVIDER_RESPONSE` are fire-and-forget at fire time, but `HookManager.drain_settlement()` waits for them (bounded) before the next provider call and before `TurnComplete` (`run()` finally).
- Settlement never waits on SSE subscribers — only in-process hook tasks.
- `Message` is frozen: rewrite by replacing list entries, not mutating message fields.
- Prefer these hooks over editing `turn_loop.py` / `tool_dispatch.py` for product concerns (drive-mode tool filtering, redaction, metering).

---

### 18. Skills

**Purpose:** Doc-driven procedural knowledge; skill names are always in the system prompt (`## Skills`), agent gets the root path via `list_skills` and full procedure via `read_file` on `SKILL.md`.

**Key files:** `skills/*/SKILL.md`, `docs/skills.md`

**Invariants:** Skills are filesystem docs, not auto-executed Python (legacy loader exists but primary path is doc-driven). They live outside the writable workspace and are addressed by file tools through the `skills/` virtual prefix; writes to that prefix are rejected.

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

### 22. Computer control (desktop-only)

**Purpose:** Nine `computer_*` custom tools that act on the local machine on the user's behalf — open/reveal a file or folder, launch an app, open a URL, read/write the clipboard, list/find files, move/rename, trash. Built for the Monkeybot desktop app ("open my Downloads folder"); irrelevant to server/Cloud Run deployments.

**Key files:** `computer/__init__.py` (env gate, tool registry), `computer/safety.py` (hard security boundary), `computer/tools.py` (the `CustomTool` implementations), `computer/approvals.py` (durable "Always allow" JSON store), `computer/permissions.py` (layered ruleset + `ComputerAwarePermissionInspector`).

**How it works:**
- Off by default, macOS-only: `computer.enabled: true` in `monkeybot.yaml` → `MONKEYBOT_COMPUTER_TOOLS` env, combined with a hard `sys.platform == "darwin"` check (`should_enable_computer_tools()`). The desktop app sets the env var when it spawns a gateway with the feature turned on in Settings; no other deployment should ever set it.
- **Hard security boundary lives in the tool bodies, not in `permissions.yaml`** (`safety.py`): every path is resolved and validated against the user's home directory after following symlinks; a fixed denylist blocks credential directories (`.ssh`, `.aws`, keychains, browser profiles, the app's own config) both at their canonical location *and* by directory-name anywhere in the tree; filenames matching credential patterns (`.env`, `*.pem`, `id_rsa*`, …) are always refused; `computer_open` refuses any path/app that would make `open` execute code (`.command`, `.app`, `.sh`, Terminal, script editors, …); trash never hard-deletes. `permissions.yaml` is fail-open (a broken file silently disables it), so none of this can depend on it.
- **Every `computer_*` call asks by default**, via a built-in baseline rule (`COMPUTER_BASELINE_RULES` in `permissions.py`) — not a line in `permissions.yaml`, so it can't be silently defeated by a missing/broken config file. `ComputerAwarePermissionInspector` layers, last-match-wins: baseline `ask` < durable approvals overlay `allow` < the user's `permissions.yaml` (highest authority — a hand-written `deny` always wins).
- **"Always allow" is durable and narrow**: `monkeybot_config/approvals.json` (machine-written, JSON — deliberately *not* appended into `permissions.yaml`, which is a comment-heavy file the app's Advanced settings save wholesale) stores one exact `(tool, resource)` pair per rule. Mutating tools (`computer_move`, `computer_trash`) are excluded from `ALWAYS_SCOPE` — their resource is the *source* path only, so an "always" rule would cover any destination — every call to them asks. The inspector re-`stat`s the overlay file on every check and reloads on change; on a genuine change it also clears the session-level approval cache, so a revoke in the app's Settings takes effect immediately rather than waiting for a gateway restart.
- Registered via the standard `extra_tools` extension point in both the SSE (`gateway/sse/app.py`) and realtime (`gateway/realtime/routes.py`) gateways — same mechanism as `web_search`/`todo_list`. Never registered for subagents (`subagent_worker.py` builds its own `extra_tools` independently and never imports this package).

**Depends on:** `AgentLayout.approvals_path` (`MONKEYBOT_APPROVALS_CONFIG`, default `monkeybot_config/approvals.json`), `TurnContext.approvals_persist` (threaded from `build_context` into `tool_dispatch.py`/`realtime_loop.py`'s `remember_always_approval(..., persist=...)`).

**Invariants:**
- Default off; zero behavior change for any deployment that doesn't set `MONKEYBOT_COMPUTER_TOOLS`.
- `resource_for_call` gained `url`/`app` argument lookups (lowest priority, after `path`) so `computer_open_url`/`computer_open_app` get readable permission resource strings; no existing tool's resource string changes.
- A resource stored in `approvals.json` is `glob.escape`d before becoming an `fnmatch` pattern, so a literal `*`/`?`/`[` in a filename can't over-match.
- `ComputerAwarePermissionInspector` is used **instead of** (not alongside) the plain `PermissionInspector` when computer tools are enabled; every other deployment's inspector chain is unchanged.

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
| MCP servers | Static `mcp.json` + `enable_mcp` / `disable_mcp` | `MCPClient` |
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
| **Doom loop** | Text loop: identical name+args streak ≥ `DOOM_LOOP_THRESHOLD` (ok or error) → Error + no-tools recovery; re-arms after each recovery; `ToolDef.doom_loop_exempt` skips (e.g. `loop_status`) |
| **Empty completion** | No text + no tools: after tools → note and retry (silent) until turn budget; otherwise up to 2 recovery re-calls (silent) then exhausted Error and end |
| **Truncated tools** | Text loop: `Done.truncated` or all-`parse_error` → fail all; realtime: all-`parse_error` only (no Live length-limit signal) |
| **Compaction summary** | Middle-history summary uses fixed Markdown template (Objective / Details / Work State / Next Move / Files); stored as `[Context Summary]:` |
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
| **Config** | Env beats yaml except YAML-only `model.*`; secrets in `.env` only |
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
| Max inner turns | 50 | `model.max_turns` (YAML only) |
| Doom-loop threshold | 3 (0 = off) | `DOOM_LOOP_THRESHOLD` |
| Context window | 200000 (example yaml: 1M) | `model.context_window` (YAML only) |
| Summarization trigger | ratio from compression config | `SUMMARY_TRIGGER_RATIO` |
| Read max lines | 5000 | `tools.read_max_lines` (YAML only — no env override) |
| Default read lines | 2000 | Harness-fixed (`AGENT_READ_DEFAULT_LINES`); pass `limit` to request more |
| Spill threshold / inline / read char budgets | derived from `model.context_window` | not configurable (no YAML, no env) |
| Context curation timeout | 10s | `context_curation.timeout_sec` |
| Current request cap | 8000 chars | code constant |
| Emission style | off | `MONKEYBOT_EMISSION_STYLE` / `emission.style` |
| Pending response timeout | 300s | `gateway.pending_response_timeout_sec` |
| Subagent timeout | 600s | `subagents.timeout_sec` |
| Subagent max turns | 25 | `subagents.max_turns` |
| Steer queue depth | 8 | `MONKEYBOT_STEER_QUEUE_MAX` |
| Follow-up queue depth | 16 | `MONKEYBOT_FOLLOW_UP_QUEUE_MAX` |
| Follow-up lock retry interval | 1s | `MONKEYBOT_FOLLOW_UP_LOCK_RETRY_S` |
| Follow-up lock wait budget | session-turn stale ms | `MONKEYBOT_FOLLOW_UP_LOCK_WAIT_MS` |
| Hook settlement timeout | 2s | `MONKEYBOT_HOOK_SETTLEMENT_TIMEOUT_S` |
| Parallel-safe tool concurrency | 10 | `MONKEYBOT_PARALLEL_TOOL_CONCURRENCY` |
| Parallel `task` concurrency | 10 | code constant (`_MAX_CONCURRENT_SUBAGENTS`) |

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

Separate harness at repo root for higher-level agent evaluation, run as a CLI against a
live gateway: `uv run python -m evals.report --suite smoke` (see `evals/report.py`; diff
two runs with `python -m evals.diff`).

---

## Module quick reference

| Concern | Start here |
|---------|------------|
| Loop / turn semantics | `src/monkeybot/core/runtime/loop.py` (`run` facade), `turn_loop.py`, `tool_dispatch.py` |
| System prompt | `src/monkeybot/core/prompts/prompt.py`, `harness_prompt.py` |
| Gateway wiring | `src/monkeybot/gateway/sse/app.py` |
| Tool dispatch (executor) | `src/monkeybot/core/tools/core_tool_executor.py` |
| Tool batch (loop) | `src/monkeybot/core/runtime/tool_dispatch.py`, `tool_batch.py` |
| Config | `core/config/runtime_env.py`, `cli/src/monkeybot_cli/scaffold_defaults/monkeybot.example.yaml` |
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
