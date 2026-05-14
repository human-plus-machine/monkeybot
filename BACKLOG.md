# MonkeyBot Backlog

---

## This Week — Harness (May 12–16)

Focus: make the core agent harness the best it can be.

### Bugs

- **Agent loop stops after tool call** — loop exits prematurely after the first tool execution; needs investigation and fix.

### Features

- **Actively evolving system prompt** — system prompt should update dynamically as the agent learns/runs (e.g. injecting memory, context, available skills at runtime); AGENT.md should stay focused on bot identity, not harness internals. (Let [agent.md](http://agent.md) be the system prompt for the agent?)
- **Dedicated harness system prompt** — separate the built-in tool/skill descriptions from the per-bot AGENT.md; harness injects its own context so bot authors don't have to re-document internals. ( we should make this only for the fixed tools that come with the harness)
- **Memory accuracy verification** — add ability to verify that saved memories are accurate and surface discrepancies (hallucinated or stale memories).
- **save_memory tool** — add or consolidate a dedicated `save_memory` tool for writing facts to the memory dir and updating `INDEX.md`; decide vs routing through `write_file` to keep the tool surface minimal; see Under Discussion for open questions. *(discussion needed)*
- **Memory summarization approach** — prefer a lightweight LLM for summarization and logging memory over a heavy council-style flow where that tradeoff makes sense.
- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` (research what Claude Code uses as a reference); cross-check the marketing bot repo and API server patterns aligned with Cursor-style read/write.
- **History summarization** — conversation history grows unbounded; implement a summarization pass (rolling window or threshold-triggered) so long-running tasks don't blow the context window.
- **Token counting before provider calls** — check estimated token count against model context window before each `provider.stream()` call; truncate or summarize history proactively rather than hitting a hard API error.
- **Fix unbounded tool results** — tool results (e.g. `read_file` on a large file) are appended to history at full length; cap or truncate large results before writing to SQLite/history.
- **Fix memory index stale mid-turn** — `memory/INDEX.md` is snapshotted once at `build_context()` time; if the agent writes to it during a turn the update is invisible until the next user message; fix by re-reading index before each provider call or adding a lightweight refresh path.
- **Fix parallel subagent result ordering** — concurrent `task` calls append to history as they finish, not in `call_id` order; this can confuse the model on the next turn; collect all results then append in deterministic order.
- **Fix subagent cancellation propagation** — when the parent's `cancelled` event is set, child subprocesses keep running until the 600s timeout; send `SIGTERM` to child PIDs on cancellation.
- **Custom subagents** — allow operators to pre-configure named subagent profiles (own AGENT.md, restricted skill set, specific MCP servers); parent model can delegate to a named profile rather than a generic subprocess.

### Maintenance

- **Documentation cleanup** — update README and any other docs to reflect the current codebase layout; remove stale references to legacy paths.

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

