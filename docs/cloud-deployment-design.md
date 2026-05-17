# MonkeyBot Cloud Deployment Design

**Status:** In progress — Steps **1**, **1.5**, and **2** (workspace / memory storage) are implemented in code; later steps (Docker baseline, deployment guides, harness-as-library hardening) remain.  
**Purpose:** Single source of truth for the multi-cloud deployment work. Open this in any new chat before starting a step to ensure nothing in that step breaks a later one.

---

## Architecture Model

The design formalizes a layer separation that already exists in the code but is not yet enforced:

```
┌──────────────────────────────────────────────────────────┐
│                    GATEWAY LAYER                          │
│  FastAPI SSE — one deployment option, not the only one   │
│  User-owned handler for Lambda / AgentCore / AgentEngine │
└──────────────────────────────────────────────────────────┘
                        │ calls
┌──────────────────────────────────────────────────────────┐
│                    HARNESS LAYER                          │
│  run_loop() + CoreToolExecutor + build_context()         │
│  MCP client + LLM providers + memory hook                │
│  No I/O opinions — pure async Python                     │
│  No global singletons, no background tasks               │
└──────────────────────────────────────────────────────────┘
                        │ depends on (via protocols)
┌───────────────────────────┐  ┌──────────────────────────┐
│      STORAGE LAYER        │  │   WORKSPACE/MEMORY LAYER  │
│  SQLite (default, zero    │  │  Local FS (default)       │
│  extra deps)              │  │  GCS ([gcs] extra)        │
│  Postgres ([postgres]     │  │  S3 ([aws] extra)         │
│  extra)                   │  │                           │
└───────────────────────────┘  └──────────────────────────┘
```

The harness layer has **zero opinions about I/O backends**. It receives storage and workspace implementations via constructor arguments — it never reads from globals or env vars directly to get them. The gateway (FastAPI) and user-defined handlers (Lambda, AgentCore) are both callers of the harness, injecting their chosen backends.

---

## Non-Negotiable Design Constraints

These apply to **every step**. Violating any of them creates a design debt that must be paid in a later step.

1. **No global singletons in the harness layer.** `_deps` in `app.py` is fine because it lives in the gateway layer. Nothing in `monkeybot.core.*` may use a module-level mutable singleton.

2. **No background tasks in the harness layer.** Startup tasks (memory GC, schema apply) belong in the gateway's lifespan handler. Lambda and AgentCore have no "startup" — the handler is the process.

3. **Image stays lean by default.** The base image installs zero cloud-provider SDKs. GCS support requires `pip install "monkeybot[gcs]"`. Postgres support requires `pip install "monkeybot[postgres]"`. A user running locally with SQLite and local filesystem pays zero extra dependencies.

4. **All storage and workspace backends are injected, not auto-resolved.** A factory function reads `DB_URL` or `WORKSPACE_STORAGE` env vars and returns the right implementation. This is called once at startup by the gateway or by the user's handler. The harness never calls the factory itself.

5. **`run_loop()` must remain callable from a short-lived process.** No assumptions about process lifetime. A Lambda handler that calls `run_loop()` once and returns must work correctly.

6. **OpenSandbox is always an external service.** MonkeyBot's config is always `SANDBOX_ENABLED + SANDBOX_SERVER_URL`. Where opensandbox runs is a deployment concern, not a harness concern. Authentication to opensandbox is network-layer (VPC/private subnet) by default, with an optional `SANDBOX_AUTH_TOKEN` env var forwarded as a Bearer header if needed.

---

## Deployment Pattern Classification

All deployment targets fall into one of three patterns. Guides are written per pattern, with thin per-target addenda covering what differs (auth, managed DB names, IAM syntax, etc.).

### Pattern A — Managed Container (FastAPI SSE gateway, long-lived process)

Run the Docker image directly. The gateway handles SSE, sessions, and the agent loop. Storage and memory backends are set via env vars.

Sandbox: either a sidecar (if the platform supports multi-container tasks) or an external service in the same private network.

| Target | Cloud | Notes |
|---|---|---|
| Local Docker Compose | Any | Baseline, SQLite + local FS |
| GCP Cloud Run | GCP | Scale-to-zero, no Docker socket (sandbox must be external) |
| GKE | GCP | Kubernetes, persistent volume or GCS memory |
| GCE (VM) | GCP | Full Docker control, sandbox sidecar works |
| AWS ECS (Fargate or EC2) | AWS | Multi-container task, sandbox sidecar works on EC2 launch type |
| EKS | AWS | Kubernetes, same pattern as GKE |
| EC2 (VM) | AWS | Full Docker control, sandbox sidecar works |
| Azure Container Apps | Azure | Same as Cloud Run pattern |
| AKS | Azure | Same as GKE/EKS pattern |
| Azure VM | Azure | Same as EC2/GCE pattern |
| NVIDIA DGX / NIM-compatible host | Any | Run as a container, same pattern as GCE/EC2 |
| Cloudflare Containers | Cloudflare | Emerging product — same container pattern when Python is supported |

### Pattern B — FaaS / Serverless (harness-as-library, short-lived invocation)

User writes their own handler. They import `monkeybot.core`, wire storage and workspace backends, call `run_loop()`, return collected events. No FastAPI, no SSE. Storage must support short-lived processes (no persistent connection pool assumption).

Sandbox: not supported. Lambda/functions have no Docker socket and hard execution time limits.

| Target | Cloud | Notes |
|---|---|---|
| AWS Lambda | AWS | Handler per invocation, Postgres or DynamoDB |
| Azure Functions | Azure | Same pattern as Lambda |
| Cloudflare Workers | Cloudflare | Python support via Workers + WASM or Containers; harness-as-library pattern |
| GCP Cloud Functions | GCP | Same pattern as Lambda |

### Pattern C — Agent Platform (harness-as-library, platform-specific runtime contract)

Same as Pattern B in terms of harness usage, but the platform imposes its own request/response contract, tool registration format, or session model. User writes a thin adapter that maps the platform's contract to `run_loop()`.

Sandbox: not supported by the platform natively; same constraint as Pattern B.

| Target | Cloud | Notes |
|---|---|---|
| GCP Vertex AI Agent Engine | GCP | Platform manages session routing; adapter maps to run_loop() |
| AWS Bedrock AgentCore | AWS | Platform manages tool invocation protocol; adapter maps to run_loop() |

---

## Deployment Target Matrix (Summary)

| Target | Pattern | Storage | Memory/Workspace | Sandbox |
|---|---|---|---|---|
| Local Docker Compose | A | SQLite (default) | Local FS (default) | Sidecar |
| GCP Cloud Run | A | Postgres (Cloud SQL) | GCS | External GCE/GKE |
| GKE | A | Postgres (Cloud SQL) or SQLite+PVC | GCS or PVC | Sidecar pod |
| GCE (VM) | A | SQLite or Postgres | Local or GCS | Sidecar container |
| AWS ECS | A | Postgres (RDS) | S3 | Sidecar (EC2 launch type) |
| EKS | A | Postgres (RDS) or SQLite+PVC | S3 or PVC | Sidecar pod |
| EC2 (VM) | A | SQLite or Postgres | Local or S3 | Sidecar container |
| Azure Container Apps | A | Postgres (Azure DB) | Azure Blob (future `[azure]` extra) | External |
| AKS / Azure VM | A | Postgres | Local or Blob | Sidecar |
| NVIDIA / other container | A | Postgres | GCS or S3 | External or sidecar |
| AWS Lambda | B | Postgres (RDS Proxy) | S3 | None |
| Azure Functions | B | Postgres | Azure Blob (future) | None |
| GCP Cloud Functions | B | Postgres (Cloud SQL) | GCS | None |
| AWS Bedrock AgentCore | C | Postgres | S3 | None |
| GCP Agent Engine | C | Postgres | GCS | None |

---

## Step 1: Storage Abstraction

**Goal:** Make SQLite and Postgres interchangeable via a single env var. Zero extra dependencies for SQLite users.

### What changes

Define two protocols in `monkeybot.core.persistence`:

- `HistoryStore` — `load(thread_id, limit) → list[Message]`, `append(thread_id, message)`, `reset(thread_id, messages)`
- `UsageStore` — `record(thread_id, model, usage, run_id)`, `summary(thread_id, since_ms) → UsageSummary`

Define a `StorageBackend` that owns connection lifecycle and exposes both stores:
- `async def open() → None` — opens connection/pool
- `async def close() → None` — closes connection/pool
- `history() → HistoryStore`
- `usage() → UsageStore`

Define a factory `create_storage_backend(db_url: str) → StorageBackend`:
- `db_url` starts with `sqlite://` → `SQLiteStorageBackend` (uses `aiosqlite`, zero new deps)
- `db_url` starts with `postgresql://` or `postgres://` → `PostgresStorageBackend` (uses `asyncpg`, gated behind `[postgres]` extra; the factory normalizes `postgres://` to `postgresql://` for asyncpg)

The gateway's lifespan (`app.on_event("startup")`) calls `create_storage_backend(os.environ["DB_URL"])` once and stores it in `app.state`. The `GatewayLoopPort.start_turn()` retrieves it from `app.state` and passes `backend.history()` and `backend.usage()` into the harness. No connection is opened per-turn.

**Managed Postgres (SSL):** Cloud SQL, RDS, and similar often require TLS. Use whatever your provider documents for libpq-style URLs (for example `sslmode=require` / `ssl=true` query params or `sslrootcert=…` on `DB_URL`). MonkeyBot passes the URL through to asyncpg unchanged after scheme normalization.

### What does NOT change

- SQLite concrete types remain the default implementations (`SQLiteHistoryStore`, `SQLiteUsageStore`; `ConversationHistory` is kept as a backwards-compat alias where applicable).
- `DB_URL` env var stays as-is. `sqlite:///data/monkeybot.db` keeps working exactly as today.
- `monkeybot.yaml` `db_url` key stays as-is.

### Image impact

- Default (SQLite): zero new packages.
- Postgres: `asyncpg` added only when user installs `[postgres]` extra.

### What this unlocks

Cloud Run and ECS users can point `DB_URL` at a Cloud SQL or RDS instance and get durable history + usage tracking without any other changes.

### Watch out for in subsequent steps

The `StorageBackend` lifecycle (open/close) must be handled in every entry point: the FastAPI gateway lifespan, the Lambda handler cold start, and any AgentCore handler. Document this clearly in the harness-as-library step.

Subagent run persistence is covered by Step 1.5 (`RunStore` + `StorageBackend.runs()`); the old “raw connection only” gap in Step 1 is closed there.

---

## Step 1.5: Run Store Abstraction

**Goal:** Complete the storage abstraction by covering `DurableRunStore` (subagent run tracking). After this step, `StorageBackend` vends all three stores: history, usage, and runs. No monkeybot core module holds a raw `aiosqlite.Connection`.

### What changes

Define a `RunStore` protocol in `monkeybot.core.persistence`:

- `create(envelope, run_id, scratch_dir) → SubagentRunRow`
- `update_status(run_id, status, result_json, error_json, finished_at) → None`
- `get(run_id) → SubagentRunRow | None`
- `list_pending(parent_run_id) → list[SubagentRunRow]`

Add `runs() → RunStore` to the `StorageBackend` protocol.

Implementations:
- `SQLiteRunStore` — wraps the existing `DurableRunStore` logic from `durable_runs.py`. `DurableRunStore` class is renamed `SQLiteRunStore`; its `aiosqlite.Connection` argument is provided by `SQLiteStorageBackend`.
- `PostgresRunStore` — asyncpg-backed, standard SQL (no SQLite-specific syntax).

Wire `SQLiteStorageBackend.runs()` and `PostgresStorageBackend.runs()` to return their respective implementations after `open()`.

Update `subagent_worker.py` to use `backend.runs()` instead of passing the raw connection to `DurableRunStore` directly. Any other call site that constructs `DurableRunStore(conn)` directly is updated to use `backend.runs()`.

### What does NOT change

- `runs.py` (ULID generation and scratch directory helpers) — pure filesystem, no DB, untouched.
- `SubagentRunRow` dataclass shape — unchanged.

### Tests

- Both `SQLiteRunStore` and (if asyncpg installed) `PostgresRunStore` against the `RunStore` protocol.
- `create` → `get` round-trip.
- `update_status` reflected by `get`.
- `list_pending` returns only rows with status `pending` or `running`.

---

## Step 2: Workspace / Memory Storage Abstraction

**Goal:** Pluggable durable memory (`WorkspaceStorage`: local FS, GCS, S3) with a single façade (`MemorySubsystem`) injected into the harness. **Primary config is YAML** (`paths.memory_storage_uri`); the gateway mirrors that into internal env `MEMORY_STORAGE_URI` for subprocess workers — not a user-facing “set this env” requirement for local dev.

### What shipped

- **`WorkspaceStorage`** protocol (`read_text`, `write_text`, `append_text`, `exists`, `list_files`, `delete`, `move`, `gc_prefix`) under `monkeybot.core.workspace`.
- **`LocalWorkspaceStorage`** — `pathlib` + `asyncio.to_thread`; `gc_prefix` performs the old processed-file sweep (non-recursive under the prefix directory).
- **`GCSWorkspaceStorage`** / **`S3WorkspaceStorage`** — sync SDKs in worker threads; `append_text` is read-merge-write (safe only under the shared memory asyncio lock); `gc_prefix` returns zeros with logs pointing to bucket lifecycle rules.
- **`create_workspace_storage(uri)`** — `local://`, bare path, `gcs://`, `s3://`; lazy imports with explicit `ImportError` messages for missing extras.
- **`MemorySubsystem`** — owns storage, constructs `MemoryHook` + `MemoryOrganizer` internally; exposes `uri`, `load_index`, `search_files`, `promote`, `gc_processed`, `register_hooks`. Call sites use `memory: MemorySubsystem | None` on `TurnContext` and `CoreToolExecutor` (not raw paths).
- **Runtime env** — `paths.memory_storage_uri` → `MEMORY_STORAGE_URI`; legacy `paths.memory_path` → `MEMORY_PATH`. Gateway `_memory_storage_uri()` prefers `MEMORY_STORAGE_URI`, else wraps `MEMORY_PATH` as `local://…` (INFO log when that legacy fallback is used).
- **Subagents** — envelope field `memory_storage_uri`; worker reads `MEMORY_STORAGE_URI` and rebuilds storage.

### What does NOT change

- **Skills** stay path-based / image-baked for now; not part of `WorkspaceStorage`.
- **Sandbox workspace** (agent code + data inside the sandbox) remains separate from durable memory.

### Image impact

Unchanged from prior plan: default image has no cloud SDKs; `[gcs]` / `[aws]` extras add provider clients.

### Watch-outs (Step 2)

1. **Lock** — `MemoryHook` owns an `asyncio.Lock`; cloud `append_text` is not atomic; hook + organizer must share the same storage instance constructed by `MemorySubsystem`.
2. **`list_files` contract** — keys are POSIX relative paths; filtering in `search_files` / organizer assumes consistent prefixes (e.g. `raw/processed/`).
3. **Subagent URI** — prefer `MEMORY_STORAGE_URI` only; avoid leaving duplicate legacy env vars in worker startup.
4. **Cloud `move`** — copy+delete may leave duplicates on partial failure; organizer may re-process; log at WARNING if delete fails after copy.
5. **`[gcs]` extra** — shared with other features; keep version pins compatible.

### What this unlocks

Cloud Run / ECS can point YAML at `memory_storage_uri: gcs://…` or `s3://…` for durable memory without a PVC.

---

## Step 3: Docker Artifacts

**Goal:** Clean up the existing Docker inconsistencies and establish a working baseline for all server-based deployments.

### Current problems

1. No base `docker-compose.yml` exists — the sandbox overlay (`docker-compose.sandbox.yml`) references one that isn't there.
2. `deploy.sh` builds from the root `Dockerfile` but should use `Dockerfile.playground` for Cloud Run (the playground Dockerfile has the correct workspace layout).
3. Two Dockerfiles with diverging layouts — likely collapse into one once storage/workspace are env-var driven (which they will be after Steps 1 and 2).
4. `Dockerfile` still references old comments about `requirements.txt` and old paths (noted in BACKLOG).

### What changes

After Steps 1 and 2, the Docker image needs no cloud-SDK deps baked in by default. The build arg `EXTRAS` handles everything:

- `EXTRAS=gemini` → default, no cloud SDKs
- `EXTRAS=gemini,gcs` → adds GCS memory support
- `EXTRAS=gemini,postgres` → adds Postgres storage support
- `EXTRAS=vertex,gcs,postgres` → full Cloud Run / GKE production setup

Create a base `docker-compose.yml` in the repo root:
- `monkeybot` service with env file support and a local volume for `data/`
- `DB_URL` defaults to SQLite
- `MEMORY_STORAGE_URI` defaults to local

The sandbox overlay stays as-is (it already works conceptually, just needs a base to overlay on).

### Regarding `deploy.sh`

The current `deploy.sh` is for internal Auriga use and deploys to the `aurigaos` project. When the repo is open-sourced, this should become a **deployment guide** (Markdown), not a script. The guide covers the same steps: build image, push to registry, deploy to Cloud Run with the right env vars and secrets. Users adapt it to their own project. Remove `deploy.sh` from the repo (or move it to `internal/`) before open-sourcing.

### What this unlocks

A working local Docker Compose baseline that users can run on their laptop, then adapt for ECS task definitions or Cloud Run without changes to the image.

---

## Step 4: Deployment Guides (Not Code)

**Goal:** Document all deployment targets as guides. Not scripts bundled with the repo. Organized by pattern so new targets (e.g. a future cloud provider) can be added by following the right pattern guide without a full new document.

### Guide structure

Three pattern guides (the core content):

**`docs/deploy-pattern-a-container.md`** — Pattern A: Managed Container
- What env vars to set and why
- How to build the image with the right `EXTRAS`
- How to connect to a managed Postgres DB
- How to connect memory to GCS or S3
- Standard opensandbox deployment (see below)
- Per-target addenda covering: IAM/permissions syntax, managed DB service names, secret management service names
  - GCP Cloud Run
  - GKE
  - GCE (VM)
  - AWS ECS
  - EKS
  - EC2 (VM)
  - Azure Container Apps / AKS / Azure VM (thin section — same pattern, different IAM syntax)
  - NVIDIA / other container hosts (one paragraph — "same as EC2/GCE")

**`docs/deploy-pattern-b-serverless.md`** — Pattern B: FaaS / Serverless
- How to import and call the harness without FastAPI
- StorageBackend lifecycle in a short-lived process
- Per-target addenda:
  - AWS Lambda
  - GCP Cloud Functions
  - Azure Functions
  - Cloudflare (note: Python support status)

**`docs/deploy-pattern-c-agent-platform.md`** — Pattern C: Agent Platform
- How to write a platform adapter wrapping `run_loop()`
- Per-target addenda:
  - AWS Bedrock AgentCore
  - GCP Vertex AI Agent Engine

### Standard OpenSandbox pattern (applies to all Pattern A targets)

There is one model: opensandbox runs as a service, monkeybot points at it via `SANDBOX_SERVER_URL`. Where opensandbox runs is infrastructure, not monkeybot config.

| Infrastructure | Where opensandbox runs |
|---|---|
| Local Docker Compose | Sidecar in docker-compose (already in repo) |
| ECS (EC2 launch type) | Sidecar container in same task definition (localhost) |
| EKS / GKE | Sidecar container in same pod (localhost) |
| GCE / EC2 / Azure VM | Same host, sidecar container (localhost) |
| Cloud Run / ECS Fargate / Container Apps | Separate VM/node in same VPC — set `SANDBOX_SERVER_URL` to private IP |

Authentication: network-layer (VPC/private subnet) by default. If opensandbox must be reachable across a network boundary, set `SANDBOX_AUTH_TOKEN` in monkeybot's env; monkeybot forwards it as `Authorization: Bearer <token>`. Configure opensandbox to require it on its end.

### Regarding `deploy.sh`

The current `deploy.sh` targets the internal `aurigaos` GCP project. Before open-sourcing, remove it from the repo (or move to `internal/`). Its content becomes the per-target addendum in the Cloud Run section of the Pattern A guide.

---

## Step 5: Harness-as-Library

**Goal:** Users who want to deploy on Lambda, AgentCore, or AgentEngine can import `monkeybot.core` directly and wire their own gateway handler. No FastAPI required.

### What this means for the harness

After Steps 1 and 2, the harness receives `StorageBackend` and `WorkspaceStorage` as injected arguments. `build_context()` and `run_loop()` have no implicit I/O dependencies. This means they're already callable from any entry point.

What needs to be documented (and potentially cleaned up):

1. `build_context()` and `run_loop()` must not start background tasks internally.
2. The memory GC that currently runs in the gateway's startup event stays in the gateway — it must not be called from the harness itself.
3. No module-level code in `monkeybot.core.*` may have side effects (no auto-connecting, no auto-loading config on import).

### What users write

A Lambda handler looks like:

```
cold start:
  backend = create_storage_backend(os.environ["DB_URL"])
  await backend.open()
  workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
  mcp = MCPClient(); await mcp.load_from_config(...)
  provider = GeminiProvider()

per-invocation:
  ctx = await build_context(session_id, request_id, ..., storage=backend, workspace=workspace)
  events = []
  async for evt in run_loop(message, ctx, provider=provider, ...):
      events.append(evt)
  return events  # caller formats response as needed
```

An AgentCore or AgentEngine handler follows the same pattern with its own request/response wrapping.

### What we provide

- Working examples under `examples/`:
  - `examples/lambda/handler.py` — AWS Lambda (Pattern B)
  - `examples/cloud-functions/main.py` — GCP Cloud Functions (Pattern B)
  - `examples/agentcore/handler.py` — AWS Bedrock AgentCore (Pattern C)
  - `examples/agentengine/handler.py` — GCP Vertex AI Agent Engine (Pattern C)
- Documentation via the three pattern guides (Step 4)
- A `monkeybot.core.bootstrap` helper (optional) that takes a config dict and returns `(StorageBackend, WorkspaceStorage, MCPClient, Provider)` — reduces boilerplate in user handlers while keeping the harness itself clean

### Constraints that Step 5 enforces on all prior steps

- Steps 1 and 2 must not assume an event loop exists at import time
- StorageBackend and WorkspaceStorage must be usable in short-lived processes (open → use → close in one handler invocation, e.g. Lambda)
- No `asyncio.create_task()` calls in harness-layer code (background tasks break in Lambda)

---

## Open Questions (Resolved)

| Question | Decision |
|---|---|
| Google Drive memory backend? | Out of scope. Not worth the complexity for the use case. |
| `deploy.sh` in open-source repo? | No. Replace with a deployment guide (Markdown). Move to `internal/` or remove before open-sourcing. |
| Single Dockerfile vs two? | Collapse to one after Steps 1+2. `EXTRAS` build arg handles provider and backend selection. |
| OpenSandbox auth? | Network-layer (VPC) by default. Optional `SANDBOX_AUTH_TOKEN` env var for Bearer token. |
| DynamoDB / Firestore storage backends? | Out of scope for initial design. Postgres covers the managed DB need on both clouds. Add later if there's demand. |
| Connection-per-turn (current SQLite pattern) vs persistent pool (Postgres)? | `StorageBackend` owns lifecycle and manages this internally. Gateway opens it at startup, closes at shutdown. Lambda opens it at cold start, closes after invocation. |

---

## Backlog Items This Design Supersedes

The following items in `BACKLOG.md` are covered by this design:

- **GCP server** — covered by Cloud Run guide (Step 4) and potentially a GKE variant
- **AWS serverless** — covered by Step 5 (harness-as-library) with Lambda example
- **AWS server** — covered by ECS guide (Step 4)
- **Docker image** — covered by Step 3
- **Postgres** — covered by Step 1

---

## Implementation Sequence

```
Step 1: Storage Abstraction
  - Define HistoryStore + UsageStore protocols           DONE
  - Define StorageBackend with open/close lifecycle      DONE
  - Implement SQLiteStorageBackend (no new deps)         DONE
  - Implement PostgresStorageBackend ([postgres] extra)  DONE
  - Factory: create_storage_backend(db_url)              DONE
  - Wire into gateway lifespan + GatewayLoopPort         DONE
  - Wire into subagent_worker.py                         DONE
  - Rename db.py → sqlite.py                             DONE
  - Rename ConversationHistory → SQLiteHistoryStore      DONE
  - Rename UsageStore (class) → SQLiteUsageStore         DONE
  - Tests: SQLiteStorageBackend + factory                DONE

Step 1.5: Run Store Abstraction
  - Define RunStore protocol                               DONE
  - Rename DurableRunStore → SQLiteRunStore              DONE
  - Implement PostgresRunStore                           DONE
  - Add runs() to StorageBackend protocol + impls        DONE
  - Update subagent_worker.py to use backend.runs()      DONE
  - Tests: both backends against RunStore protocol       DONE

Step 2: Workspace / Memory Storage Abstraction
  - Define WorkspaceStorage protocol                               DONE
  - Implement LocalWorkspaceStorage (no new deps)                  DONE
  - Implement GCSWorkspaceStorage ([gcs] extra)                    DONE
  - Implement S3WorkspaceStorage ([aws] extra)                     DONE
  - Factory: create_workspace_storage(uri)                         DONE
  - MemorySubsystem façade + harness wiring                        DONE
  - YAML paths.memory_storage_uri (+ runtime env mirror)           DONE
  - Backwards-compat: MEMORY_PATH → local:// URI                   DONE
  - Tests: local + factory + protocol contract + memory fakes      DONE

Step 3: Docker Artifacts
  - Consolidate Dockerfile (after Step 1+2 land)
  - Create base docker-compose.yml
  - Remove internal deploy.sh or move to internal/
  - Verify sandbox overlay still works against new base

Step 4: Deployment Guides (three pattern guides + per-target addenda)
  - docs/deploy-pattern-a-container.md
      Addenda: Cloud Run, GKE, GCE, ECS, EKS, EC2,
               Azure Container Apps/AKS/VM, NVIDIA/other
  - docs/deploy-pattern-b-serverless.md
      Addenda: AWS Lambda, GCP Cloud Functions,
               Azure Functions, Cloudflare (status note)
  - docs/deploy-pattern-c-agent-platform.md
      Addenda: AWS Bedrock AgentCore, GCP Agent Engine

Step 5: Harness-as-Library
  - Audit core/* for global singletons and background tasks
  - Add optional monkeybot.core.bootstrap helper
  - examples/lambda/handler.py           (Pattern B)
  - examples/cloud-functions/main.py     (Pattern B)
  - examples/agentcore/handler.py        (Pattern C)
  - examples/agentengine/handler.py      (Pattern C)
```
