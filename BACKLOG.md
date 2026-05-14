# MonkeyBot Backlog

---

## This Week — Harness (May 12–16)

Focus: make the core agent harness the best it can be.

Tasks are split into two parallel tracks. **Do not edit files outside your track without coordinating first.**

---

### Track A — Loop & History (John)
> **Owns:** `core/loop.py`, `core/history.py`, `core/context.py`

---

#### Branch: `bugs/loop-correctness`

- **Bug: Agent loop stops after tool call** — loop exits prematurely after the first tool execution; investigate and fix in `_run_inner`.
- **Fix parallel subagent result ordering** — concurrent `task` calls append to history as they finish, not in `call_id` order; collect all results then append in deterministic order (`loop.py` ~line 393).

---

#### Branch: `feat/context-window-safety` *(implemented)*

- **Fix unbounded tool results** — large tool returns spill to `.monkeybot/spill/{thread_id}/{call_id}.txt` with capped in-history text + path hint; spill dir cleaned at next `run()` start when `workspace_root` is set (`core_tool_executor.py`, `loop.py`).
- **Token counting before provider calls** — `_estimate_tokens` on full provider payload vs `TurnContext.context_window_tokens` (default 200k; gateway passes `MODEL_CONTEXT_WINDOW`); trigger at 85% (`loop.py`, `context.py`).
- **History summarization** — sync summarization via extra `provider.stream` call, `history.reset()`, and UI events `ContextSummarizing` / `ContextSummarized` (`loop.py`, `history.py`, `events.py`).
- **`.monkeybot` write scope** — paths under `.monkeybot` bypass `WORKSPACE_WRITE_SCOPE_REL` so spill and harness files remain writable (`workspace_service.py`).

---

#### Branch: `feat/memory-index-refresh` *(implemented)*

- **Fix memory index stale mid-turn** — `memory/INDEX.md` is snapshotted once at `build_context()` time; fixed by `refresh_memory_index()` in `context.py` + `memory_path` on `TurnContext`, invoked before each main `provider.stream` in `loop.py` (`load_index()` API; no `memory.py` changes).

---

#### Branch: `feat/lazy-loading` *(optional — needs profiling first)*

- **? Lazy loading** — import providers/skills/tools only when invoked to improve cold-start speed and avoid loading unused dependencies; needs profiling to confirm it's worth the complexity.

---

### Track B — Memory, Prompts, Tool Executor & Providers (Karthik)
> **Owns:** `core/memory.py`, `core/harness_prompt.py`, `core/core_tool_executor.py`, `core/workspace_tools.py`, `core/subagent_proto.py`, `core/subagent_worker.py`, `core/config.py`, `core/memory_organizer.py`, `core/interfaces.py`, `providers/`, `README`

- **Actively evolving system prompt** — `compose_system_prompt()` in `prompt.py` should inject memory, context, and available skills at runtime; AGENT.md stays focused on bot identity, not harness internals.
- **Memory accuracy verification** — add ability to verify saved memories are accurate and surface discrepancies (hallucinated or stale) in `memory.py`.
- **save_memory tool** — add `_tool_save_memory` to `core_tool_executor.py` for writing facts to the memory dir and updating `INDEX.md`; decide vs routing through `write_file` to keep tool surface minimal. *(discussion needed)*
- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` in `core_tool_executor.py` and `workspace_tools.py` (reference: Claude Code patterns).
- **Custom subagents** — allow operators to pre-configure named subagent profiles (own AGENT.md, restricted skill set, specific MCP servers) in `core_tool_executor.py`, `subagent_proto.py`, and `subagent_worker.py`.
- **Documentation cleanup** — update README to reflect current codebase layout; remove stale references to legacy paths.

---

### ⚠️ Interface Contract

Track A calls into Track B's modules but does **not** modify them. Track B must keep these signatures stable (new params must be additive/optional):

| Owned by Track B | Called by Track A |
|---|---|
| `memory.load_index(memory_path)` | `context.py` refresh before each provider call |
| `memory.search_memory(query, memory_path)` | `context.py` / `loop.py` context building |
| `harness_prompt.harness_fixed_context(*, include_task_tool)` | `loop.py` `_system_message()` |

If Track B needs to change any of these signatures, coordinate with Track A before merging.

---

## Next Week — Connectors & Deployments (May 19–23)

Focus: plug the harness into real messaging surfaces and cloud runtimes.

### Connectors

- **Scheduler gateway wiring** — wire `Scheduler` into the FastAPI lifespan as an optional background task when `config.yaml` has `scheduler.jobs` (reference: `legacy/src/monkeybot/core/scheduler.py`; copy or reintroduce module from legacy when implementing).
- **Google Chat gateway** — incoming webhook + event handler for Google Chat spaces.
- **Slack gateway** — Slack Events API / socket mode integration.
- **CLI gateway** — interactive stdin/stdout (reference: `legacy/src/monkeybot/gateway/cli.py`).
- **Webhook gateway** — generic HTTP + HMAC (reference: `legacy/src/monkeybot/gateway/webhook.py`).
- `**python -m monkeybot` CLI** — `run` / `serve` / `usage` / `schedule` subcommands (reference: `legacy/src/monkeybot/cli.py`).

### Cloud Deployments

- **GCP serverless** — Cloud Run deployment guide + config.
- **GCP server** — GCE / GKE deployment option.
- **AWS serverless** — Lambda + API Gateway deployment.
- **AWS server** — EC2 / ECS deployment option.
- **Docker image** — align Dockerfile with current layout (`pyproject.toml`, `python -m monkeybot.gateway.main`, `.agents/skills/`); currently references old `requirements.txt` / `skills/` / `src.main` paths.

### Infra

- **Postgres** — swap SQLite for history/usage where needed.

### Traceability / Observability / Evals

langfuse? deepeval other?
---

## Backlog (Unscheduled)

### Runtime / Safety

- **Memory index refresh after summarization (optional)** — After summarization, `_run_inner` rebuilds `provider_messages` with the same `system` built before `_summarize_history`; low risk today (summarize path does not touch `INDEX.md`) but consider `ctx = await refresh_memory_index(ctx)` plus `_system_message(ctx)` before that rebuild for consistency and future hooks (`loop.py`).
- **Configurable summarization model** — history compression in `loop.py` currently uses the same `ctx.model` as the agent; allow a separate model id (env or `TurnContext`) for the summarization-only `provider.stream` call.
- **HITL completion** — ApprovalRequest/Response loop (inspector `approve` path).
- **DurableRunStore wiring** — persist `task` / subagent runs in `core_tool_executor.py` for crash recovery.

### MCP + Distro

- **MCP distro linkage** — confirm `bots/example-bot/` env (`MCP_CONFIG`, `SKILLS_PATH`) matches deployment; smoke-test MCP load against real servers beyond the bundled LangChain docs URL.
