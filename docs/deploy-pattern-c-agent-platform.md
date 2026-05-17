# Pattern C — Agent Platform Deployment

Same harness-as-library approach as Pattern B, but the platform imposes its own request/response contract, tool registration format, or session model. You write a thin adapter that maps the platform's contract to `run_loop()`.

**Targets covered:** AWS Bedrock AgentCore · GCP Vertex AI Agent Engine

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

AWS Bedrock AgentCore manages session routing and invokes your handler per agent turn. The platform provides the session ID and user message; your handler returns a response object in AgentCore's schema.

**IAM — execution role permissions:**

| Permission | Why |
|---|---|
| `secretsmanager:GetSecretValue` | Read DB URL and API keys |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` | S3 memory bucket |
| `bedrock:InvokeModel` | Call Bedrock-hosted models (if using Bedrock provider) |
| `rds-db:connect` | RDS IAM auth (optional) |

**Handler (`handler.py`):**

```python
import asyncio, os
from monkeybot.core.persistence import create_storage_backend
from monkeybot.core.workspace import create_workspace_storage
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.harness import build_context, run_loop

# Cold start
_backend = create_storage_backend(os.environ["DB_URL"])
asyncio.get_event_loop().run_until_complete(_backend.open())
_workspace = create_workspace_storage(os.environ["MEMORY_STORAGE_URI"])
_provider = GeminiProvider()


def handler(event, context):
    """
    AgentCore invokes this once per agent turn.
    `event` contains the AgentCore session ID and the user's input text.
    Adjust field names to match the AgentCore runtime contract for your API version.
    """
    session_id = event["sessionId"]
    message = event["inputText"]

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

    # Collect the final assistant message to return as the AgentCore response.
    # AgentCore expects a plain text response in `actionGroupOutput.text` for
    # inline agents, or a structured response for action groups.
    assistant_messages = [
        e.content for e in events
        if hasattr(e, "role") and e.role == "assistant"
    ]
    response_text = assistant_messages[-1] if assistant_messages else ""

    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "apiPath": event.get("apiPath"),
            "httpMethod": event.get("httpMethod"),
            "httpStatusCode": 200,
            "responseBody": {
                "application/json": {
                    "body": response_text
                }
            },
        },
    }
```

**Environment variables:**

```
DB_URL             = postgresql://user:pass@rds-proxy:5432/monkeybot?sslmode=require
MEMORY_STORAGE_URI = s3://my-bucket/monkeybot-memory
GEMINI_API_KEY     = (from Secrets Manager)
```

**AgentCore registration:** Register this handler as a Lambda function backing an AgentCore action group, or as an inline agent Lambda. Refer to the [Bedrock AgentCore documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core.html) for the exact registration API — the request/response envelope above follows the v1 action group contract but may evolve.

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
