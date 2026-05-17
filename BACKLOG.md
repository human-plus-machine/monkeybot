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

- **Bug: Agent loop stops after tool call** — loop exits prematurely after the first tool execution; investigate and fix in `_run_inner`. *(pending — awaiting logs to reproduce)*

---

### Track B — Memory, Prompts, Tool Executor & Providers (Karthik)
> **Owns:** `core/memory.py`, `core/harness_prompt.py`, `core/core_tool_executor.py`, `core/workspace_tools.py`, `core/subagent_proto.py`, `core/subagent_worker.py`, `core/config.py`, `core/memory_organizer.py`, `core/interfaces.py`, `providers/`, `README`

- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` in `core_tool_executor.py` and `workspace_tools.py` (reference: Claude Code patterns).

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
- **Sandbox workspace protection** *(unscheduled — depends on sandbox above)* — After sandbox ships, add a hard-coded deny layer inside `SandboxExecutor` that blocks `run_command` from targeting harness-owned paths: `.monkeybot/`, `bot.yaml`, `*.env`, `.agents/`, `config/`. Path-level policy at the executor (different from `command_allowlist.yaml` which is command-level). Prevents the agent from clobbering memory index or harness config via shell while allowing free rw access to `./code/`, `./data/`, etc.
- **Memory index refresh after summarization (optional)** — After summarization, `_run_inner` rebuilds `provider_messages` with the same `system` built before `_summarize_history`; low risk today (summarize path does not touch `INDEX.md`) but consider `ctx = await refresh_memory_index(ctx)` plus `_system_message(ctx)` before that rebuild for consistency and future hooks (`loop.py`).
- **HITL completion** — ApprovalRequest/Response loop (inspector `approve` path).
- **DurableRunStore wiring** — persist `task` / subagent runs in `core_tool_executor.py` for crash recovery.

### MCP + Distro

- **MCP distro linkage** — confirm `bots/example-bot/` env (`MCP_CONFIG`, `SKILLS_PATH`) matches deployment; smoke-test MCP load against real servers beyond the bundled LangChain docs URL.

### Other backlog items

- **INDEX.md size cap (Lever 4)** — *(deferred 2026-05-15)* `MemoryOrganizer` appends to `INDEX.md` without bound. Today this is bounded indirectly by `ContextCurator` (LLM-side selection over the full index) at `>8` entries, which is acceptable for now. When indices grow large enough that even reading `INDEX.md` from disk is wasteful, cap with a sliding window (e.g. keep last N=200, archive older lines to `INDEX.archive.md`) in `core/memory_organizer.py`.
- **Custom subagents** — allow operators to pre-configure named subagent profiles (own AGENT.md, restricted skill set, specific MCP servers) in `core_tool_executor.py`, `subagent_proto.py`, and `subagent_worker.py`.
- **Memory accuracy verification** — add ability to verify saved memories are accurate and surface discrepancies (hallucinated or stale) in `memory.py`. (wrote a script for now but dont think its good in the long run, we need some kinda eval)
