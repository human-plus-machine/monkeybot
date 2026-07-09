# Pattern C — Agent Platform Deployment

Same harness-as-library approach as Pattern B, but the platform imposes its own request/response contract, tool registration format, or session model. You write a thin adapter that maps the platform's contract to `run_loop()`.

**Targets covered:** AWS Bedrock AgentCore · GCP Vertex AI Agent Engine

**Guide depth:** This guide documents **both** shipped platform adapters side by side. You need only the section for your target — AgentCore does not require Vertex, and vice versa. See [Positioning](cloud-deployment-design.md#positioning).

**Sandbox:** Not supported. These platforms manage their own execution environments and do not provide a Docker socket.

---

## 1. The Adapter Pattern

Every Pattern C deployment follows the same structure:

1. **Cold start** — open `StorageBackend`, init workspace, LLM provider, and MCP client.
2. **Platform entry point** — the platform calls your handler with its own request envelope.
3. **Translate** — extract `session_id` and `message` from the platform's envelope.
4. **Run** — call `build_context()` then `run_loop()`.
5. **Translate back** — collect events and format them as the platform's response envelope.

```
Platform request
      │
      ▼
  [your adapter]
      │  extract session_id, message
      ▼
  build_context()  ──►  run_loop()  ──►  [events]
                                              │
                                             [your adapter]
                                              │  format response
                                              ▼
                                     Platform response
```

The adapter is thin — it does not contain agent logic. All agent logic lives in the harness (`run_loop()`, tools, memory, MCP).

---

## 2. Dependency Installation

```bash
pip install "monkeybot[gemini,postgres,gcs]"
```

No `[sandbox]` extra needed. Use only the storage extras appropriate for your platform (GCS for GCP, S3/postgres for AWS).

---

## Per-Target Addenda

### AWS Bedrock AgentCore

AWS Bedrock AgentCore can invoke a **Lambda/action-group** handler or a **managed HTTP container** (port 8080, `/ping`, `/invocations`). MonkeyBot examples:

| Artifact | Use case |
|---|---|
| `examples/agentcore/handler.py` | Lambda / action-group JSON event |
| `examples/agentcore/runtime_app.py` | Container HTTP runtime |
| `docs/deploy-aws-agentcore.md` | CLI pitfalls, arm64, base64 payload, env files |

**IAM — execution role permissions:**

| Permission | Why |
|---|---|
| `secretsmanager:GetSecretValue` | Read DB URL and API keys |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` | S3 memory bucket |
| `bedrock:InvokeModel` | Call Bedrock-hosted models (if using Bedrock provider) |
| `rds-db:connect` | RDS IAM auth (optional) |

**Handler (bootstrap — see `examples/agentcore/handler.py`):**

```python
from pathlib import Path
from monkeybot.core.bootstrap import create_harness_deps, run_pattern_bc_turn

_deps = None

async def _ensure_deps():
    global _deps
    if _deps is None:
        _deps = await create_harness_deps(os.environ["DB_URL"], os.environ.get("MEMORY_STORAGE_URI"))
    return _deps

async def _run_turn(event):
    deps = await _ensure_deps()
    return await run_pattern_bc_turn(
        deps,
        event["inputText"],
        session_id=event["sessionId"],
        request_id=event.get("request_id", "req-1"),
        agent_md_path=Path(os.environ["AGENT_MD_PATH"]),
        skills_path=Path(os.environ["SKILLS_PATH"]),
        workspace_root=Path(os.environ["WORKSPACE_ROOT"]),
    )
```

`run_pattern_bc_turn` raises `PatternBcTurnError` on loop failures (instead of returning empty text).

**Environment variables:**

```
DB_URL             = postgresql://user:pass@rds-proxy:5432/monkeybot?sslmode=require
MEMORY_STORAGE_URI = s3://my-bucket/monkeybot-memory
AGENT_MD_PATH      = /app/monkeybot_config/AGENT.md
SKILLS_PATH        = /app/.agents/skills
WORKSPACE_ROOT     = /app
MODEL_PROVIDER     = aws_bedrock
MODEL_NAME         = (your Bedrock model id)
```

**Session continuity:** AgentCore manages session routing externally. The `sessionId` it provides maps directly to MonkeyBot's `session_id` — history and memory are keyed on it. You do not need to implement session management yourself.

---

### GCP Vertex AI Agent Engine

Vertex AI Agent Engine manages session routing and invokes your handler via its custom agent framework. You register a Python callable that the platform wraps.

**IAM — service account roles:**

| Role | Why |
|---|---|
| `roles/aiplatform.user` | Call Vertex AI models and Agent Engine APIs |
| `roles/secretmanager.secretAccessor` | Read secrets |
| `roles/storage.objectAdmin` | GCS memory bucket |
| `roles/cloudsql.client` | Cloud SQL (if using Cloud SQL) |

**Adapter (`agent_engine_handler.py`):**

```python
import asyncio, os
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

# Cold start — Agent Engine keeps the process warm between invocations.
_backend = create_storage_backend(os.environ["DB_URL"])
asyncio.get_event_loop().run_until_complete(_backend.open())
_workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
_provider = GeminiProvider()


def query(*, session_id: str, message: str, **kwargs) -> dict:
    """
    Agent Engine calls this function per turn.
    The signature must match Agent Engine's callable contract.
    `session_id` is provided by the platform's session routing.
    """
    async def _run():
        ctx = await build_context(
            session_id=session_id,
            storage=_backend,
            workspace=_workspace,
        )
        events = []
        async for evt in run_loop(message, ctx, provider=_provider):
            events.append(evt)
        return events

    events = asyncio.get_event_loop().run_until_complete(_run())

    assistant_messages = [
        e.content for e in events
        if hasattr(e, "role") and e.role == "assistant"
    ]
    return {"output": assistant_messages[-1] if assistant_messages else ""}
```

**Register and deploy with the Vertex AI SDK:**

```python
import vertexai
from vertexai.preview import reasoning_engines

vertexai.init(project=PROJECT_ID, location=REGION, staging_bucket=f"gs://{STAGING_BUCKET}")

# Wrap and deploy
app = reasoning_engines.AdkApp(
    agent=query,          # your callable
    enable_tracing=False,
)

remote_app = reasoning_engines.ReasoningEngine.create(
    app,
    requirements=["monkeybot[gemini,postgres,gcs]"],
    display_name="monkeybot",
    description="MonkeyBot via Vertex AI Agent Engine",
)
print(remote_app.resource_name)
```

**Query the deployed agent:**

```python
remote_app.query(
    session_id="user-123-session-456",
    message="What files are in my workspace?",
)
```

**Session continuity:** Agent Engine provides the `session_id`. MonkeyBot uses it as the history and memory key — no additional session management is needed in your adapter.

**Environment variables:** Set these on the reasoning engine at creation time via `env_vars` in the SDK, or read them from Secret Manager at cold start using the service account's credentials.

**Note:** The Vertex AI Agent Engine (formerly Reasoning Engine) API surface is evolving. Check the [Vertex AI Agent Engine documentation](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview) for the current callable registration contract before deploying to production.
