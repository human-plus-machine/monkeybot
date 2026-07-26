# monkeybot Backlog

---

## Up Next — Major Priorities

### 1. Chat Integration
- **Google Chat gateway** — incoming webhook + event handler for Google Chat spaces.
- **Slack gateway** — Slack Events API / socket mode integration.

### 2. Sandbox Workspace Protection
- Add a hard-coded deny layer inside `SandboxExecutor` that blocks `run_command` from targeting harness-owned paths: `.monkeybot/`, `monkeybot.yaml`, `*.env`, `.agents/`, `config/`.
- Path-level policy at the executor — distinct from `command_allowlist.yaml` which is command-level. Agent should retain free rw access to `./code/`, `./data/`, etc.

### 3. CLI-managed optional service dependencies
- `monkeybot chat` / `run` only spawn the gateway process; they don't start the Docker-backed optional services some `monkeybot.yaml` configs require: OpenSandbox (`sandbox.enabled`), Firestore emulator (`paths.db_url: firestore://...`), or the observability stack (Phoenix/Langfuse/OTel collector). Those used to be orchestrated by a demo `run.sh`; the CLI alone still can't fully run an agent that uses those features.
- Decide and implement: should the CLI (`doctor`, `run`, `chat`) detect these from config and actually start/stop the containers, or just validate/warn with remediation hints while the user manages Docker manually?
- Open sub-questions if going the "CLI starts services" route: where do per-project Docker assets live (`opensandbox.docker.toml`, OTel collector yaml, compose files) — agent-project-owned files referenced from `monkeybot.yaml`, or CLI-shipped generic templates; and is the sandbox worker image (project-specific extra deps) something the CLI should build, or stay manual/documented.

---

## Bugs

- **Corrupted memory recovery** — investigate how memory can end up corrupted (e.g. bad `INDEX.md` / episodic notes / encoding failures) and add a repair path: detect corruption, recover or quarantine bad files, and restore a usable memory index without manual surgery. (Image generation use case)

---

## Do Later

### Connectors
- **CLI gateway** — interactive stdin/stdout beyond the existing `monkeybot chat` / `talk` clients.
- **Webhook gateway** — generic HTTP + HMAC ingress for third-party chat products.
- **Expanded `python -m monkeybot` surface** — additional operator subcommands (`usage` / `schedule`, etc.) as needed.

### Cloud Deployments
- **GCP server** — GCE / GKE deployment option (docs + examples exist; no IaC).
- **AWS serverless** — Lambda + API Gateway (example handler exists; not a full deploy path).
- **AWS server** — EC2 / ECS deployment option.
- **Docker CI image matrix** — multi-extras build matrix in CI.

### Infra
- **Postgres as production default** — backend exists; gateway still defaults to SQLite; decide when/if this becomes the default.
- **Firestore usage aggregation doesn't scale** — `FirestoreUsageStore.summary()`/`breakdown()` (agent-wide, no `thread_id`) stream the entire `turn_usage` collection into memory and aggregate in Python. Fine at small scale; revisit with a Firestore aggregation query or a scheduled rollup doc if `/usage` traffic or data volume grows.
- **Knowledge ANN index (sqlite-vec)** — deliberately deferred. The numpy matrix cache in `sqlite_vector.py` holds through ~50–100k chunks and query cost is embed-API bound, not local ANN. Revisit only if measured ANN p95 exceeds ~20–50 ms or the resident matrix causes RAM pressure; the true scale escape hatch is `store.type: pgvector`, not `sqlite-vec` packaging.

### MCP
- **MCP distro linkage** — confirm scaffolded `monkeybot.yaml` paths (`paths.mcp_config`, `paths.skills_path`) match deployment; smoke-test against real MCP servers beyond the bundled examples.

### Tooling *(intentionally dropped — not completed)*

Product decision to stop tracking these as active backlog items (not claiming they shipped):

- **File-op tool audit** — Keep `write_file`; no plan to replace it with `create_file` + `find_and_replace` only.
- **Custom subagents** — Named profiles with per-persona skills / MCP selections remain out of scope for now; workers continue to load the global skills path and `MCP_CONFIG`.

### Future platforms *(not scheduled)*

Moved from README — longer-term / speculative integrations without active implementation:

- **Azure OpenAI** — Azure-hosted OpenAI models
- **Azure Blob Storage** — memory backend (`[azure]` extra; docs note as planned)
- **Azure Key Vault** — secrets for Azure deployments
- **AWS Secrets Manager** — secret resolver at runtime
- **GCP Secret Manager** — finish `secrets.provider: gcp_secret_manager` wiring (schema exists; resolver not shipped)
- **Microsoft Teams** — Teams bot interface
- **Telegram** — Telegram bot interface
- **DynamoDB** — checkpointer / job storage backend
- **Cosmos DB** — Azure-native persistence options
