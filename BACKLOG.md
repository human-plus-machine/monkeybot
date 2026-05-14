# MonkeyBot Backlog

---

## This Week — Harness (May 12–16)

Focus: make the core agent harness the best it can be.

### Bugs
- **Agent loop stops after tool call** — loop exits prematurely after the first tool execution; needs investigation and fix.

### Features
- **Actively evolving system prompt** — system prompt should update dynamically as the agent learns/runs (e.g. injecting memory, context, available skills at runtime); AGENT.md should stay focused on bot identity, not harness internals.
- **Dedicated harness system prompt** — separate the built-in tool/skill descriptions from the per-bot AGENT.md; harness injects its own context so bot authors don't have to re-document internals.
- **Memory accuracy verification** — add ability to verify that saved memories are accurate and surface discrepancies (hallucinated or stale memories).
- **save_memory tool review** — decide: keep `save_memory` as a dedicated tool, or route through `write_file`? Goal is to keep the tool surface minimal and not overload the agent. Lean toward fewer, more deliberate tools.
- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` (research what Claude Code uses as a reference).

### Maintenance
- **Documentation cleanup** — update README and any other docs to reflect the current codebase layout; remove stale references to legacy paths.

---

## Next Week — Connectors & Deployments (May 19–23)

Focus: plug the harness into real messaging surfaces and cloud runtimes.

### Connectors
- **Google Chat gateway** — incoming webhook + event handler for Google Chat spaces.
- **Slack gateway** — Slack Events API / socket mode integration.
- **CLI gateway** — interactive stdin/stdout (reference: `legacy/src/monkeybot/gateway/cli.py`).
- **Webhook gateway** — generic HTTP + HMAC (reference: `legacy/src/monkeybot/gateway/webhook.py`).
- **`python -m monkeybot` CLI** — `run` / `serve` / `usage` / `schedule` subcommands (reference: `legacy/src/monkeybot/cli.py`).

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
