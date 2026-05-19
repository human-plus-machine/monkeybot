# MonkeyBot Backlog

---

## Up Next — Major Priorities

### 1. Chat Integration
- **Google Chat gateway** — incoming webhook + event handler for Google Chat spaces.
- **Slack gateway** — Slack Events API / socket mode integration.

### 2. Evals *(in progress)*
- Integrate eval framework (langfuse, deepeval, or similar) into the harness — not just playground-level; needs to be first-class traceability in the agent loop.
- **Memory accuracy verification** — script exists (`scripts/verify_memory.py`) but needs a real eval strategy, not a one-off script.

### 3. Scheduler
- Wire a `Scheduler` into the FastAPI lifespan as an optional background task when `config.yaml` has `scheduler.jobs` (reference: `legacy/src/monkeybot/core/scheduler.py`).
- Needs significant design work for cloud runtimes: Lambda/Cloud Functions have no persistent process, GKE/ECS can use sidecar or CronJob, etc. Think through heartbeat / self-scheduling before implementing.

### 4. Sandbox Workspace Protection
- Add a hard-coded deny layer inside `SandboxExecutor` that blocks `run_command` from targeting harness-owned paths: `.monkeybot/`, `bot.yaml`, `*.env`, `.agents/`, `config/`.
- Path-level policy at the executor — distinct from `command_allowlist.yaml` which is command-level. Agent should retain free rw access to `./code/`, `./data/`, etc.

### 5. HITL Completion
- The loop and `/tool-confirmations` API endpoint exist; inspectors have `confirm` as a valid `Decision.kind` but no inspector currently returns it ("Story 5" placeholder).
- Wire an inspector that returns `confirm` for configurable tool patterns, flowing through the existing `_await_user_response` path.

### 6. DurableRunStore Wiring (crash recovery)
- `SQLiteRunStore` and `PostgresRunStore` exist on the storage backend but subagents do not persist runs through `core_tool_executor` — no calls into `backend.runs()` in `subagent_worker.py`.
- Wire subagent launch/completion/failure into the run store so a crashed harness can recover task state.

---

## Do Later

### Connectors
- **CLI gateway** — interactive stdin/stdout (reference: `legacy/src/monkeybot/gateway/cli.py`).
- **Webhook gateway** — generic HTTP + HMAC (reference: `legacy/src/monkeybot/gateway/webhook.py`).
- **`python -m monkeybot` CLI** — `run` / `serve` / `usage` / `schedule` subcommands (reference: `legacy/src/monkeybot/cli.py`).

### Cloud Deployments
- **GCP server** — GCE / GKE deployment option (docs + examples exist; no IaC).
- **AWS serverless** — Lambda + API Gateway (example handler exists; not a full deploy path).
- **AWS server** — EC2 / ECS deployment option.
- **Docker CI image matrix** — multi-extras build matrix in CI.

### Infra
- **Postgres as production default** — backend exists; gateway still defaults to SQLite; decide when/if this becomes the default.

### Memory
- **INDEX.md size cap** *(deferred 2026-05-15)* — `MemoryOrganizer` appends without bound; acceptable for now via `ContextCurator` selection. When indices grow wastefully large, cap with a sliding window (N=200, archive to `INDEX.archive.md`) in `core/memory_organizer.py`.

### MCP
- **MCP distro linkage** — confirm `bots/example-bot/` env (`MCP_CONFIG`, `SKILLS_PATH`) matches deployment; smoke-test against real MCP servers beyond the bundled LangChain docs URL.

### Tooling
- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` in `core_tool_executor.py` and `workspace_tools.py` (reference: Claude Code patterns).
- **Custom subagents** — allow operators to pre-configure named subagent profiles (own AGENT.md, restricted skill set, specific MCP servers) in `core_tool_executor.py`, `subagent_proto.py`, and `subagent_worker.py`.
