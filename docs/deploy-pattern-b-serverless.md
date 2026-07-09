# Pattern B — FaaS / Serverless Deployment

Import `monkeybot.core` directly in your own handler. No FastAPI, no SSE, no long-lived process. You wire storage and workspace backends, call `run_loop()`, collect events, and return them in whatever format the platform expects.

**Targets covered:** AWS Lambda · GCP Cloud Functions · Azure Functions · Cloudflare Workers

**Sandbox:** Not supported on any FaaS platform. Functions have no Docker socket and hard execution time limits. Disable sandbox (`SANDBOX_ENABLED=false` or omit it).

---

## 1. Harness-as-Library Pattern

The harness has no opinions about I/O. It receives `StorageBackend` and `WorkspaceStorage` as arguments and calls `run_loop()` — a plain async generator. Your handler is the gateway.

### Minimal handler structure

```python
import asyncio
import os
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.mcp_client import MCPClient
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

# Cold-start init — runs once per container/instance lifetime.
# Open connections here; they are reused across invocations on warm instances.
_backend = None
_workspace = None
_mcp = None
_provider = None

async def _cold_start():
    global _backend, _workspace, _mcp, _provider
    _backend = create_storage_backend(os.environ["DB_URL"])
    await _backend.open()
    _workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
    _mcp = MCPClient()
    await _mcp.load_from_config(os.environ.get("MCP_CONFIG_PATH"))
    _provider = GeminiProvider()

asyncio.get_event_loop().run_until_complete(_cold_start())

# Per-invocation handler
async def handle(session_id: str, message: str) -> list[dict]:
    ctx = await build_context(
        session_id=session_id,
        storage=_backend,
        workspace=_workspace,
    )
    events = []
    async for evt in run_loop(message, ctx, provider=_provider, mcp=_mcp):
        events.append(evt)
    return events
```

---

## 2. StorageBackend Lifecycle in Short-Lived Processes

FaaS platforms reuse warm instances (containers) across invocations but may terminate them at any time. The lifecycle rules:

- **`backend.open()`** — call once at cold start (module level or first-invocation guard). Opens the connection pool.
- **`backend.close()`** — call in a shutdown hook if your platform provides one (e.g. Lambda `atexit`), or accept that the pool is cleaned up when the container exits.
- **Never call `open()` per invocation** — this creates a new pool on every request and exhausts Postgres connection limits quickly.

**On short-lived platforms where every invocation is a fresh process** (some Function configs, very aggressive scale-to-zero):

```python
async def handle(session_id, message):
    backend = create_storage_backend(os.environ["DB_URL"])
    await backend.open()
    try:
        ctx = await build_context(session_id=session_id, storage=backend, ...)
        events = [evt async for evt in run_loop(message, ctx, ...)]
        return events
    finally:
        await backend.close()
```

Opening and closing per invocation adds ~20–50 ms of latency and connection churn. Prefer Postgres connection poolers (RDS Proxy, Cloud SQL Proxy, PgBouncer) when running in this mode.

---

## 3. Dependency Installation

Install monkeybot with only the extras you need:

```bash
# Gemini + Postgres + GCS memory
pip install "monkeybot[gemini,postgres,gcs]"

# Gemini + Postgres + S3 memory
pip install "monkeybot[gemini,postgres,aws]"
```

No `[sandbox]` extra is needed — sandbox is not supported on FaaS.

---

## Per-Target Addenda

### AWS Lambda

**Runtime:** Python 3.12. Package as a container image (recommended for larger deps) or a zip with a layer.

**IAM — execution role permissions:**

| Permission | Why |
|---|---|
| `secretsmanager:GetSecretValue` | Read DB URL and API keys from Secrets Manager |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` | S3 memory bucket |
| `rds-db:connect` | RDS IAM auth (optional) |

**Handler:**

```python
# lambda_function.py
import asyncio, os
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

_backend = create_storage_backend(os.environ["DB_URL"])
asyncio.get_event_loop().run_until_complete(_backend.open())
_workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
_provider = GeminiProvider()

def lambda_handler(event, context):
    session_id = event["session_id"]
    message = event["message"]

    async def _run():
        ctx = await build_context(session_id=session_id, storage=_backend, workspace=_workspace)
        return [evt async for evt in run_loop(message, ctx, provider=_provider)]

    events = asyncio.get_event_loop().run_until_complete(_run())
    return {"events": [e.model_dump() for e in events]}
```

**Environment variables (set in Lambda console or IaC):**

```
DB_URL          = postgresql://user:pass@rds-proxy.endpoint:5432/monkeybot?sslmode=require
MEMORY_STORAGE_URI = s3://my-bucket/monkeybot-memory
GEMINI_API_KEY  = (from Secrets Manager via a Lambda extension or fetched at cold start)
```

**Timeout:** Lambda max is 15 minutes. Set your function timeout to match the longest expected agent run (agent turns can take 30–120 seconds depending on tools and LLM latency).

**Connection pooling:** Use RDS Proxy in front of RDS Postgres. Lambda cold starts create new DB connections; RDS Proxy absorbs the churn.

**Packaging as a container image:**

```dockerfile
FROM public.ecr.aws/lambda/python:3.12
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY lambda_function.py .
CMD ["lambda_function.lambda_handler"]
```

---

### GCP Cloud Functions

**Runtime:** Python 3.12. Use 2nd gen Cloud Functions (supports longer timeouts and more memory).

**IAM — service account roles:**

| Role | Why |
|---|---|
| `roles/secretmanager.secretAccessor` | Read secrets |
| `roles/aiplatform.user` | Call Vertex AI (if using Vertex provider) |
| `roles/storage.objectAdmin` | GCS memory bucket |
| `roles/cloudsql.client` | Cloud SQL (if using Cloud SQL) |

**Handler (`main.py`):**

```python
import asyncio, os, functions_framework
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

_backend = create_storage_backend(os.environ["DB_URL"])
asyncio.get_event_loop().run_until_complete(_backend.open())
_workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
_provider = GeminiProvider()

@functions_framework.http
def monkeybot_handler(request):
    payload = request.get_json()
    session_id = payload["session_id"]
    message = payload["message"]

    async def _run():
        ctx = await build_context(session_id=session_id, storage=_backend, workspace=_workspace)
        return [evt async for evt in run_loop(message, ctx, provider=_provider)]

    events = asyncio.get_event_loop().run_until_complete(_run())
    return {"events": [e.model_dump() for e in events]}
```

**`requirements.txt`:**

```
monkeybot[gemini,postgres,gcs]
functions-framework
```

**Deploy:**

```bash
gcloud functions deploy monkeybot-handler \
  --gen2 \
  --runtime python312 \
  --region us-central1 \
  --source . \
  --entry-point monkeybot_handler \
  --trigger-http \
  --allow-unauthenticated \
  --memory 512MB \
  --timeout 300s \
  --set-env-vars DB_URL=postgresql://...,MEMORY_STORAGE_URI=gcs://... \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest
```

**Connection pooling:** Use Cloud SQL Auth Proxy or the Cloud SQL connector URL format:

```
postgresql+asyncpg://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
```

---

### Azure Functions

**Runtime:** Python 3.11+ (v2 programming model recommended).

**IAM — Managed Identity roles:**

| Role | Why |
|---|---|
| Key Vault Secrets User | Read secrets from Key Vault |
| Storage Blob Data Contributor | Azure Blob memory (when `[azure]` extra is available) |

**Handler (`function_app.py`):**

```python
import azure.functions as func
import asyncio, os
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

app = func.FunctionApp()

_backend = create_storage_backend(os.environ["DB_URL"])
asyncio.get_event_loop().run_until_complete(_backend.open())
_workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
_provider = GeminiProvider()

@app.route(route="monkeybot", methods=["POST"])
async def monkeybot_handler(req: func.HttpRequest) -> func.HttpResponse:
    payload = req.get_json()
    session_id = payload["session_id"]
    message = payload["message"]

    ctx = await build_context(session_id=session_id, storage=_backend, workspace=_workspace)
    events = [evt async for evt in run_loop(message, ctx, provider=_provider)]
    return func.HttpResponse(
        body=str([e.model_dump() for e in events]),
        mimetype="application/json",
    )
```

**`requirements.txt`:**

```
monkeybot[gemini,postgres]
azure-functions
```

**Note:** Azure Blob Storage memory backend (`[azure]` extra) is not yet released. Use a Postgres-only setup with `MEMORY_STORAGE_URI=local:///tmp/memory` for temporary local memory, or connect to an S3-compatible endpoint if available.

---

### Cloudflare Workers

**Status:** Python support on Cloudflare Workers is available via Workers + WASM or Cloudflare Containers (in beta as of 2026). The pattern applies when Python is supported in your target Cloudflare product.

**Constraints specific to Cloudflare:**
- No `asyncio` event loop in the traditional sense — use Cloudflare's async handler model.
- No persistent file system — `MEMORY_STORAGE_URI` must point to R2 (Cloudflare's S3-compatible object store) or an external S3/GCS bucket.
- No persistent TCP connections between invocations — open and close `StorageBackend` per invocation (see Section 2).
- `DB_URL` must point to a Hyperdrive-proxied Postgres connection or an external managed Postgres.

**Handler pattern (Cloudflare Containers / Workers Python):**

```python
# handler.py — adapt to Cloudflare's actual Python runtime API when GA
import os
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

async def on_fetch(request, env):
    payload = await request.json()
    backend = create_storage_backend(env.DB_URL)
    await backend.open()
    workspace = create_workspace_storage(env.MEMORY_STORAGE_URI)
    provider = GeminiProvider(api_key=env.GEMINI_API_KEY)
    try:
        ctx = await build_context(
            session_id=payload["session_id"],
            storage=backend,
            workspace=workspace,
        )
        events = [evt async for evt in run_loop(payload["message"], ctx, provider=provider)]
        return Response(json={"events": [e.model_dump() for e in events]})
    finally:
        await backend.close()
```

Verify Python runtime availability and API surface against the [Cloudflare Workers Python docs](https://developers.cloudflare.com/workers/languages/python/) before building for production.
