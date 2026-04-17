# Creating an agent with the Agent Harness

This guide is the **harness-first** path: you compose a declarative
`HarnessConfig` (see `src/core/harness/specs.py` in this repository),
call `build_universal_agent`, and mount the FastAPI routers. For the older
`bot.yaml` + `build_deep_agent` path used by many Google Chat bots, see
[`creating-an-agent.md`](creating-an-agent.md).

## Prerequisites

- Python 3.11+
- `pip install 'emonk[harness-full]'` (or `pip install -e ".[dev]"` from a clone)
- Identity markdown files (SOUL, RULES, …) under a directory you control

## 1. Choose a backend stack

Use [`docs/harness/backend-matrix.md`](harness/backend-matrix.md) to pick
checkpointer, memory store, job storage, identity source, secret resolver, and
model provider. For local development, start with in-memory / JSON / env
defaults from the matrix.

## 2. Author `harness.yaml`

A minimal file loads the agent, wires identity from disk, and picks sane
defaults for sandbox and security:

```yaml
agent:
  name: my-harness-agent
  model: gemini-2.5-flash
  provider: google_vertexai

identity:
  dir: ./data/memory
  enforce_rules: true

security:
  principal_required: true

sandbox:
  backend: local_shell
```

Add extension blocks as you graduate toward production, for example:

```yaml
checkpointer:
  backend: in_memory

memory_store:
  backend: in_memory

job_storage:
  backend: json

secret_resolver:
  backend: env

model_provider:
  backend: vertex
```

See [`docs/agent-harness.md`](agent-harness.md) for every top-level key and
[`docs/extending-the-harness.md`](extending-the-harness.md) for custom backends.

## 3. Drop identity markdown

Under `identity.dir`, create at least:

- `SOUL.md`, `RULES.md`, `IDENTITY.md`, `USER.md`, `INDEX.md`, `MEMORY.md`, `HEARTBEAT.md`

The format for `RULES.md` is documented in [`docs/agent-harness.md#identity-files`](agent-harness.md#identity-files).

## 4. Build the compiled agent

```python
from pathlib import Path

from src.core.harness import HarnessConfig, build_universal_agent
from src.core.harness.principal import make_user_principal

cfg = HarnessConfig.from_yaml(Path("harness.yaml"))
compiled = build_universal_agent(cfg)

# Optional smoke:
import asyncio

async def main():
    out = await compiled.ainvoke(
        [{"role": "user", "content": "hello"}],
        principal=make_user_principal(user_id="demo", email="user@example.com"),
    )
    print(out)

asyncio.run(main())
```

## 5. Expose HTTP (control plane + AgentCore)

Follow the pattern in [`examples/greenfield_agent/main.py`](../examples/greenfield_agent/main.py):

```python
from fastapi import FastAPI

from src.core.harness import HarnessConfig, build_universal_agent
from src.gateway.agentcore_routes import router as agentcore_router
from src.gateway.harness_routes import router as harness_router

cfg = HarnessConfig.from_yaml("harness.yaml")
compiled = build_universal_agent(cfg)

app = FastAPI()
app.include_router(harness_router)
app.include_router(agentcore_router)
app.state.compiled_agent = compiled
app.state.session_registry = compiled.session_registry
app.state.run_package_writer = compiled.run_package_writer
app.state.approval_channel = compiled.approval_channel
```

Mount identity / secrets / AWS admin routers the same way
[`src/gateway/server.py`](../src/gateway/server.py) does when the harness feature
flag is enabled.

## 6. Run locally

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
uvicorn myapp:app --host 0.0.0.0 --port 8080
```

Smoke endpoints:

- `GET /harness/introspect`
- `POST /agentcore/invocations` with the Bedrock-shaped JSON body

## 7. Deploy

- **GCP Cloud Run** — [`deploy-gcp.md`](deploy-gcp.md)
- **AWS (Bedrock + Postgres + S3 stack)** — [`docs/harness/aws-enterprise-runbook.md`](harness/aws-enterprise-runbook.md)

## 8. Extend with a custom backend

When you outgrow shipped backends, subclass the ABC, register it, and run the
contract suite from your own repo. Full instructions:
[`docs/extending-the-harness.md`](extending-the-harness.md).

## Further reading

- [`docs/agent-harness.md`](agent-harness.md) — architecture, middleware order, RunPackage
- [`docs/extending-the-harness.md`](extending-the-harness.md) — registry precedence + examples
- [`examples/greenfield_agent/`](../examples/greenfield_agent/) — runnable minimal service
