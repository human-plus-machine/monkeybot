# monkeybot Backlog

---

## Up Next — Major Priorities

### 1. Chat Integration
- **Google Chat gateway** — incoming webhook + event handler for Google Chat spaces.
- **Slack gateway** — Slack Events API / socket mode integration.

### 2. Evals *(in progress)*
- Integrate eval framework (langfuse, deepeval, or similar) into the harness — not just playground-level; needs to be first-class traceability in the agent loop.
- **Memory accuracy verification** — script exists (`scripts/verify_memory.py`) but needs a real eval strategy, not a one-off script.

### 3. Scheduler
- Wire a `Scheduler` into the FastAPI lifespan as an optional background task when `config.yaml` has `scheduler.jobs`.
- Needs significant design work for cloud runtimes: Lambda/Cloud Functions have no persistent process, GKE/ECS can use sidecar or CronJob, etc. Think through heartbeat / self-scheduling before implementing.

### 4. Sandbox Workspace Protection
- Add a hard-coded deny layer inside `SandboxExecutor` that blocks `run_command` from targeting harness-owned paths: `.monkeybot/`, `monkeybot.yaml`, `*.env`, `.agents/`, `config/`.
- Path-level policy at the executor — distinct from `command_allowlist.yaml` which is command-level. Agent should retain free rw access to `./code/`, `./data/`, etc.

### 5. HITL Completion
- The loop and `/tool-confirmations` API endpoint exist; inspectors have `confirm` as a valid `Decision.kind` but no inspector currently returns it ("Story 5" placeholder).
- Wire an inspector that returns `confirm` for configurable tool patterns, flowing through the existing `_await_user_response` path.

### 6. DurableRunStore Wiring (crash recovery) — DONE
- Subagent runs are persisted via `CoreToolExecutor` (`record_started` / `record_completed` / `record_failed`).
- Queue mode: set `MONKEYBOT_TASK_QUEUE=1` to enqueue with `record_pending` instead of inline spawn.
- Worker pool: standalone `python -m monkeybot.subagents.worker` consumes queued runs with atomic `claim()` (recommended for production — its own event loop, scales independently of the gateway). `MONKEYBOT_WORKER_POOL=1` runs the same loop in-process on the gateway and is development-only (competes with the SSE event loop).

### 7. CLI-managed optional service dependencies
- `monkeybot chat` / `run` only spawn the gateway process; they don't start the Docker-backed optional services some `monkeybot.yaml` configs require: OpenSandbox (`sandbox.enabled`), Firestore emulator (`paths.db_url: firestore://...`), or the observability stack (Phoenix/Langfuse/OTel collector). Those used to be orchestrated by a demo `run.sh`; the CLI alone still can't fully run an agent that uses those features.
- Decide and implement: should the CLI (`doctor`, `run`, `chat`) detect these from config and actually start/stop the containers, or just validate/warn with remediation hints while the user manages Docker manually?
- Open sub-questions if going the "CLI starts services" route: where do per-project Docker assets live (`opensandbox.docker.toml`, OTel collector yaml, compose files) — agent-project-owned files referenced from `monkeybot.yaml`, or CLI-shipped generic templates; and is the sandbox worker image (project-specific extra deps) something the CLI should build, or stay manual/documented.

### 8. Knowledge Layer — PR #123 review follow-ups
Source: PR #123 (knowledge layer) review feedback. Embeddings default off; evals gate everything. None blocked merge.

- **Content-aware chunking** *(top priority)* — replace the line-aligned ~700-token char window with per-suffix strategies: symbol-aware boundaries for code (tree-sitter or indent/brace heuristic; don't split inside top-level defs), heading-section boundaries for markdown/docs, top-level key groups for JSON/YAML/TOML, keep current window as prose fallback. Eval-gated; measure whether rank-4 misses move with heading-boundary chunking on markdown alone. Pull forward from F18 / Phase 2.5.
- **Vector search perf (still SQLite)** — short-term: normalize at write time, pre-slice Matryoshka dims at write (store/scan 1024 not 2048), cache unpacked matrix in memory (invalidate on upsert) + numpy matmul. Real fix: spike `sqlite-vec` (`vec0` KNN) behind existing `store.type`. Decide chunk-count ceiling (monorepo 50k+?) to choose which path is enough.
- **Embedding provider adapters** — config lists `openai | voyage | gemini | openai_compatible` but only NVIDIA is implemented; other values warn and degrade to keyword+graph. Thin `EmbeddingProvider` protocol with per-provider defaults (model, dim, prefix convention); OpenAI / `openai_compatible` nearly free given the existing OpenAI-SDK + custom base URL adapter.
- **Document-type extraction plan** — indexer is text-suffix only; PDFs, DOCX, images skipped. Design doc lists per-page PDF text + caption-then-embed for images. Make explicit: PDF-at-minimum as committed fast-follow vs someday; note "text files only in v1" in the design doc indexing section if deferred.
- **Orphan-vector self-heal** — confirm startup always runs vector reconciliation (`delete_missing` / re-embed on hash mismatch), including whether heal is gated on `startup_scan`. Add a regression test that `_ingest_ann` drops hits whose chunk snippet can't be resolved (stale orphan between crash and heal).
- **Single-writer invariant** — document prominently that one gateway process owns the knowledge DBs (currently only in docstrings), or accelerate pgvector if multi-replica / shared-storage cloud deploy is real. Confirm subagent read-only search is enforced, not just convention.

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

### Memory
- **INDEX.md size cap** *(deferred 2026-05-15)* — `MemoryOrganizer` appends without bound; acceptable for now via `ContextCurator` selection. When indices grow wastefully large, cap with a sliding window (N=200, archive to `INDEX.archive.md`) in `core/memory_organizer.py`.

### MCP
- **MCP distro linkage** — confirm scaffolded `monkeybot.yaml` paths (`paths.mcp_config`, `paths.skills_path`) match deployment; smoke-test against real MCP servers beyond the bundled examples.

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

### Tooling
- **File-op tool audit** — evaluate removing `write_file` in favor of `create_file` + `find_and_replace` in `core_tool_executor.py` (reference: Claude Code patterns).
- **Custom subagents** — allow operators to pre-configure named subagent profiles (own AGENT.md, restricted skill set, specific MCP servers) in `core_tool_executor.py`, `subagent_proto.py`, and `subagent_worker.py`.
