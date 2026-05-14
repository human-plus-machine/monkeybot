# MonkeyBot Backlog

---

## This Week — Harness (May 12–16)

Focus: make the core agent harness the best it can be.

Tasks are split into two parallel tracks. **Do not edit files outside your track without coordinating first.**

---

### Track A — Loop & History
> **Owns:** `core/loop.py`, `core/history.py`, `core/context.py`

- **Bug: Agent loop stops after tool call** — loop exits prematurely after the first tool execution; investigate and fix in `_run_inner`.
- **History summarization** — conversation history grows unbounded; implement a summarization pass (rolling window or threshold-triggered) in the `loop.py` turn cycle so long-running tasks don't blow the context window.
- **Token counting before provider calls** — check estimated token count against model context window before each `provider.stream()` call in `loop.py`; truncate or summarize history proactively rather than hitting a hard API error.
- **Fix unbounded tool results** — tool results (e.g. `read_file` on a large file) are appended to history at full length; cap or truncate large results at the point they are written to history in `loop.py`.
- **Fix parallel subagent result ordering** — concurrent `task` calls append to history as they finish, not in `call_id` order; collect all results then append in deterministic order (`loop.py` ~line 393).
- **Fix memory index stale mid-turn** — `memory/INDEX.md` is snapshotted once at `build_context()` time; fix by adding a lightweight index refresh in `context.py` before each provider call. *(No changes to `memory.py` needed — just re-call the existing `load_index()` API.)*

---

### Track B — Memory, Prompts, Tool Executor & Providers
> **Owns:** `core/memory.py`, `core/prompt.py`, `core/core_tool_executor.py`, `core/workspace_tools.py`, `core/subagent_proto.py`, `core/subagent_worker.py`, `core/config.py`, `core/council.py`, `core/interfaces.py`, `providers/`, `README`

- **Actively evolving system prompt** — `compose_system_prompt()` in `prompt.py` should inject memory, context, and available skills at runtime; AGENT.md stays focused on bot identity, not harness internals.
- **Dedicated harness system prompt** — separate built-in tool/skill descriptions from the per-bot AGENT.md in `prompt.py`; harness injects its own context for fixed tools so bot authors don't re-document internals.
- **Memory accuracy verification** — add ability to verify saved memories are accurate and surface discrepancies (hallucinated or stale) in `memory.py`.
- **save_memory tool** — add `_tool_save_memory` to `core_tool_executor.py` for writing facts to the memory dir and updating `INDEX.md`; decide vs routing through `write_file` to keep tool surface minimal. *(discussion needed)*
- **Memory summarization approach** — prefer a lightweight LLM for summarization over a heavy council-style flow in `memory.py`.
- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` in `core_tool_executor.py` and `workspace_tools.py` (reference: Claude Code patterns).
- **Fix subagent cancellation propagation** — when the parent's `cancelled` event is set, child subprocesses keep running until timeout; send `SIGTERM` to child PIDs on cancellation in `_tool_task` inside `core_tool_executor.py`.
- **Custom subagents** — allow operators to pre-configure named subagent profiles (own AGENT.md, restricted skill set, specific MCP servers) in `core_tool_executor.py`, `subagent_proto.py`, and `subagent_worker.py`.
- **Remove LangChain / complete provider migration** — drop all `langchain-*` dependencies and replace with native provider classes. **Do not replace with LiteLLM** — it is 37 MB vs the ~5 MB total LangChain stack, trading one heavy dependency for a heavier one. The codebase already has `providers/claude.py`, `providers/vertex_claude.py`, and `providers/gemini.py`; finish the migration by: (1) replacing `BaseChatModel` type hints in `config.py`, `council.py`, and `interfaces.py` with the existing `Provider` protocol; (2) replacing the `@tool` decorator import from `langchain_core.tools` in `workspace_tools.py` with a lightweight custom decorator; (3) routing all model instantiation in `config.py` through the existing provider classes; (4) removing all `langchain-*` entries from `pyproject.toml`. For Vertex and Bedrock, use native SDKs (`google-cloud-aiplatform`, `boto3`) directly inside the provider classes.
- **Documentation cleanup** — update README to reflect current codebase layout; remove stale references to legacy paths.

---

### ⚠️ Interface Contract

Track A calls into Track B's modules but does **not** modify them. Track B must keep these signatures stable (new params must be additive/optional):

| Owned by Track B | Called by Track A |
|---|---|
| `memory.load_index(memory_path)` | `context.py` refresh before each provider call |
| `memory.search_memory(query, memory_path)` | `context.py` / `loop.py` context building |
| `prompt.compose_system_prompt(...)` | `loop.py` `_system_message()` |

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

---

## Backlog (Unscheduled)

### Runtime / Safety

- **HITL completion** — ApprovalRequest/Response loop (inspector `approve` path).
- **DurableRunStore wiring** — persist `task` / subagent runs in `core_tool_executor.py` for crash recovery.

### Providers

- **LiteLLM** — evaluate replacing per-provider implementations with LiteLLM as a unified gateway; reduces maintenance surface and adds model portability.
- **AWS Bedrock** — implement `providers/bedrock.py`; `[project.optional-dependencies] bedrock` is a placeholder in `pyproject.toml`.

### Infra

- **Postgres** — swap SQLite for history/usage where needed.

### Product / Memory

- **Council** — merge legacy fixed categories with `INDEX.md` classification from Karthik's council.

### MCP + Distro

- **MCP distro linkage** — confirm `bots/example-bot/` env (`MCP_CONFIG`, `SKILLS_PATH`) matches deployment; smoke-test MCP load against real servers beyond the bundled LangChain docs URL.

### Maintenance

- **Playground lock file** — regenerate `playground/agent/uv.lock` after dependency renames (`uv lock` in that directory).

---

## Under Discussion

> Items marked `?` — worth tracking but no decision yet.

- **? Remove LangChain** — replace LangChain with direct provider calls like the legacy codebase did; reduces dependency weight and gives us full control, but is a significant refactor.
- **? Lazy loading** — import providers/skills/tools only when invoked to improve cold-start speed and avoid loading unused dependencies; needs profiling to confirm it's worth the complexity.

