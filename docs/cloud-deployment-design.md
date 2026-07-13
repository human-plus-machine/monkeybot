# Cloud deployment

monkeybot is **multi-cloud by architecture**, **GCP-first in documentation**. The harness is cloud-neutral: inject SQLite, Postgres, or Firestore for session storage; use `local://`, `gcs://`, or `s3://` for memory; pick any shipped LLM adapter. This is not a universal integration catalog — it is one owned runtime with explicit deployment patterns and optional extras per cloud SDK.

## Positioning

| Area | GCP (most detailed guides) | AWS (shipped) | Azure / other |
|---|---|---|---|
| LLM | Vertex / Gemini — primary examples | Bedrock (`monkeybot[bedrock]`) | OpenAI-compat providers where applicable |
| Object memory | GCS (`monkeybot[gcs]`) | S3 (`monkeybot[aws]`) | Azure Blob — planned ([BACKLOG](../BACKLOG.md)) |
| Session DB | Cloud SQL, Firestore | RDS + Postgres URI | Azure Database for PostgreSQL (Pattern A addendum) |
| Platform adapters | Cloud Run, Vertex AI Agent Engine | AgentCore, ECS, Lambda | Container Apps / Functions — thinner addenda |
| Local dev | SQLite + `local://` — **no cloud account required** | Same | Same |

**If you are not on GCP:** start with [Pattern A](deploy-pattern-a-container.md) or [Pattern B](deploy-pattern-b-serverless.md), set `DB_URL` and `MEMORY_STORAGE_URI` for your managed Postgres and object store, install the provider extra you need (`bedrock`, `openai`, …). GCS and Vertex are not required to run the harness.

Pattern guides often **lead with GCP** service names because that is the primary production target; the **env-var contract is the same** on AWS and Azure — swap managed-service names and IAM using each guide’s addenda.

## Architecture

```
Gateway / platform handler  →  harness (run_loop, tools, MCP, providers)
                                    ↓ injected
                         StorageBackend  +  Memory / workspace
```

- **Gateway layer** — FastAPI SSE, or a user-owned Lambda / AgentCore / Agent Engine handler.
- **Harness layer** — no I/O opinions; backends are injected; no global singletons or background tasks in `monkeybot.core.*`.
- **Storage / memory** — SQLite/Postgres/Firestore and local/GCS/S3 via extras; factory at process startup, not inside the loop.

**Constraints (all patterns):** `run_loop()` must work in short-lived processes;
all relative configuration resolves from the agent root; and no target may require
a writable path outside `workspace/` and `data/`. The scaffolded agent image
contains its own config and skills. Add cloud/provider dependencies to the
agent's `pyproject.toml`, regenerate `uv.lock`, and rebuild the agent image.

## Pattern guides

| Pattern | When | Guide |
|---|---|---|
| **A — Managed container** | Long-lived FastAPI SSE gateway (Cloud Run, ECS, K8s, VMs) | [deploy-pattern-a-container.md](deploy-pattern-a-container.md) |
| **B — FaaS / serverless** | Import harness in Lambda / Cloud Functions / Azure Functions | [deploy-pattern-b-serverless.md](deploy-pattern-b-serverless.md) |
| **C — Agent platform** | Thin adapter for AgentCore / Vertex Agent Engine | [deploy-pattern-c-agent-platform.md](deploy-pattern-c-agent-platform.md) · [AgentCore HTTP](deploy-aws-agentcore.md) |
| **D — Realtime WebSocket** | Full-duplex audio/text alongside SSE | [deploy-pattern-d-realtime.md](deploy-pattern-d-realtime.md) |

For day-to-day local runs, prefer the CLI (`monkeybot new` / gateway) rather
than Compose. The repository [`docker/Dockerfile`](../docker/Dockerfile) is for
MonkeyBot CI/demo use; build a scaffolded agent from its own `Dockerfile`.

## Canonical layout and deployment status

The agent root has four zones: read-only `monkeybot_config/`, read-only
`skills/`, agent-writable `workspace/`, and harness-managed `data/`. The
scaffolded `Dockerfile` is the production build input for an individual agent;
the repository's [`docker/Dockerfile`](../docker/Dockerfile) remains a MonkeyBot
CI/demo image. See [Agent project layout](agent-layout.md) for path resolution,
file-tool routing, and migration behavior.

“Tested locally” below means the generated agent was exercised against the
local package build. “Configuration validated” means Compose rendering was
checked; it is not a running Docker integration test. “Pattern only” is
architecture guidance rather than a managed-cloud test claim.

| Target | Status | config + skills | workspace | data | sandbox | browser |
|---|---|---|---|---|---|---|
| Local CLI | tested locally | plain directories | plain directory | SQLite + local memory | off or Compose sidecar | desktop Chrome or local headless |
| Local Docker / Compose | configuration validated | baked into agent image | anonymous volume | SQLite volume or Postgres | Compose overlay | headless Chromium in image |
| Cloud Run / ECS Fargate / Container Apps | pattern only | baked into image, read-only | ephemeral | managed DB + object memory | remote compute-only | in-image Chromium or Browser Use Cloud |
| GKE / EKS / ECS-EC2 / VM | pattern only | baked into image | volume or `emptyDir` | managed DB + object memory, or PVC | co-located Docker-socket sidecar | Chromium sidecar or in-image |
| AWS AgentCore | pattern only | handler bundle | ephemeral or platform file mount | managed session storage or URI overrides | none or remote compute-only | Browser Use Cloud |
| Vertex Agent Engine | pattern only | source artifact | ephemeral temporary storage | URI overrides | none or remote compute-only | Browser Use Cloud |

### Remote sandbox capability boundary

OpenSandbox mounts are host bind mounts, not network filesystems. A remote
sandbox cannot mount the gateway's baked-in skills or ephemeral workspace. In
that topology, sandbox execution is **compute-only**: commands exchange data via
arguments, standard input, and standard output. Mounted-path operations fail
explicitly. The Compose `/tmp/monkeybot-workspace` host-path technique is
Compose-only, not a cloud pattern.

Cloud Run's writable filesystem is in-memory, so workspace artifacts,
screenshots, and browser profiles count against instance memory. Configure
managed `DB_URL` and `MEMORY_STORAGE_URI` for state that must survive recycling.

## Multi-process storage and task queue

When several gateway or worker processes share one `DB_URL`, conversation isolation is by `thread_id`; subagent run isolation is by `run_id`.

For **queued subagent work** (`MONKEYBOT_TASK_QUEUE=1`), workers poll `pending_runs()` and `claim(run_id, worker_id)`. Stale claims use `MONKEYBOT_WORKER_STALE_CLAIM_MS` (default 10 minutes). See [Features — durable subagent runs](features.md#14-durable-subagent-runs-task-queue) for worker env vars.

- **Production:** standalone `python -m monkeybot.subagents.worker` alongside the gateway (separate event loop; share state via `DB_URL`).
- **Development only:** `MONKEYBOT_WORKER_POOL=1` co-locates a worker in the gateway process — do not use in production.
- **SQLite:** single-process / local only. **Postgres / Firestore:** multi-process worker pools.

Handle `StorageBackend` open/close in every entry point (gateway lifespan, FaaS cold start, AgentCore handler).
