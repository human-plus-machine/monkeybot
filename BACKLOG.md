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

### 4. Knowledge Layer — PR #123 review follow-ups
Source: PR #123 (knowledge layer) review feedback. Embeddings default off; evals gate everything. None blocked merge.

- ~~**Content-aware chunking** *(top priority)*~~ ✅ — per-suffix strategies in `chunking.py` (markdown headings, tree-sitter / brace heuristic for code, JSON/YAML/TOML top-level keys, prose fallback). Optional `knowledge-ast` extra. Offline markdown rank≤4 gate in `test_knowledge_chunking_rank.py`.
- ~~**Vector search perf — short-term (still SQLite)**~~ ✅ — normalize at write time (`upsert` L2-normalizes once; query no longer re-normalizes stored rows), Matryoshka dims already sliced client-side at write in `embeddings/nvidia.py` (store/scan 1024, not 2048), and an in-memory numpy matrix cache in `sqlite_vector.py` (`_MatrixCache`, invalidated on `upsert`/`delete_by_path`/`delete_missing`, rebuilt lazily on next `query`) replaces per-query struct-unpack + pure-Python dot products with a single `matrix @ query` numpy matmul. `numpy` added as a core dep (`<2.5` pin — newer stub files break `mypy` under this project's `python_version = 3.11` target).
- ~~**Vector search perf — real fix (sqlite-vec)**~~ ⏸ deferred — numpy matrix cache is enough through ~50–100k chunks; query cost is embed-API bound, not local ANN. Revisit only if measured ANN p95 > ~20–50 ms or RAM pressure from the full matrix. True scale escape hatch remains `store.type: pgvector`, not `sqlite-vec` packaging.
- ~~**Embedding provider adapters**~~ ✅ — `EmbeddingProvider` protocol + factory with per-provider defaults (model/dim/base_url/prefix/`input_type`). Implementations: NVIDIA (unchanged Matryoshka), OpenAI, `openai_compatible`, Voyage (OpenAI-compat + `input_type`), Gemini/`google` (`google-genai`). Unknown/misconfigured providers still soft-degrade to keyword+graph.
- ~~**Document-type extraction**~~ ✅ — PDF (per-page `pypdf`), DOCX (`python-docx`), and images (path caption default; optional LLM vision + hash cache). Optional `knowledge-media` extra; `knowledge.captions: off|path|llm` (default `path`). Design doc indexing section updated.
- ~~**Orphan-vector self-heal**~~ ✅ — heal confirmed: full content-hash scan (`delete_missing` + re-embed on hash mismatch) runs when `startup_scan: true` (default) or on workspace rescan; with `startup_scan: false` and embeddings on, startup still prunes vector rows absent from FTS. Query soft-drop: `_ingest_ann` skips hits whose chunk snippet can't be resolved. Regression tests in `test_knowledge_fusion_ann.py` / `test_knowledge_indexer.py`; design doc accepted-risk updated.
- ~~**Single-writer invariant**~~ ✅ — one gateway writer per workspace knowledge DB (`KnowledgeWriterConflictError` on a second live writer). Subagents open `KnowledgeSubsystem.create(..., read_only=True)` and get working `search` without indexer/hooks. Documented in design doc + features. pgvector remains Phase 3 for multi-replica.

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
