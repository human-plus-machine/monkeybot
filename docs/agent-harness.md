# Agent Harness

<!-- BEGIN harness-extensibility story 9 -->
> **Looking to ship a new backend?** For consumer-facing extension docs —
> the three registration mechanisms, worked Redis + DynamoDB examples, contract
> test hookup, and the AWS enterprise runbook — see
> [`docs/extending-the-harness.md`](extending-the-harness.md).
<!-- END harness-extensibility story 9 -->

A middleware-first, seven-pillar "operating system" around LangGraph Deep Agents.
Ship one enterprise-safe base image; every consumer team builds their domain agent on
top by composing a `HarnessConfig` in Python and wiring their own tools, skills,
evaluators, and MCP servers.

The Agent Harness adds **six pluggable extension surfaces** (checkpointer, memory
store, job storage, identity source, secret resolver, model provider) with shipped
reference backends and a single registration story. See
[`docs/extending-the-harness.md`](extending-the-harness.md) and
[`docs/harness/backend-matrix.md`](harness/backend-matrix.md).

## Table of contents

1. [Why this exists](#why-this-exists)
2. [Quickstart](#quickstart)
3. [The seven pillars](#the-seven-pillars)
4. [Extension surfaces](#extension-surfaces)
5. [`HarnessConfig` reference](#harnessconfig-reference)
6. [CompiledAgent handles](#compiledagent-handles)
7. [Middleware pipeline (frozen order)](#middleware-pipeline-frozen-order)
8. [Framework vs. consumer responsibility](#framework-vs-consumer-responsibility)
9. [Running locally](#running-locally)
10. [Deploying to Bedrock AgentCore Runtime](#deploying-to-bedrock-agentcore-runtime)
11. [Deploying to GCP Cloud Run](#deploying-to-gcp-cloud-run)
12. [Integrating Phoenix, DeepEval, and OTel](#integrating-phoenix-deepeval-and-otel)
13. [Identity files (`SOUL`, `RULES`, `IDENTITY`, …)](#identity-files)
14. [Sandbox backends & custom adapters (OpenShell)](#sandbox-backends)
15. [MCP integration](#mcp-integration)
16. [RunPackage schema and GRPO readiness](#runpackage-schema-and-grpo-readiness)
17. [Control plane HTTP API](#control-plane-http-api)
18. [CLI: `emonk-harness`](#cli-emonk-harness)
19. [Testing strategy](#testing-strategy)
20. [Versioning & migration](#versioning--migration)

---

## Why this exists

Enterprise consumers want a universal agent base that handles the seven pillars
out of the box — identity, context management, sandbox/permissions, terminal
access, skills, subagent orchestration, and evaluation/observability — while
letting them plug in their own LLM choice, their own tools, their own
observability stack (Phoenix, DeepEval, OTel), and their own HITL channel.

The **Agent Harness** is exactly that. The framework ships primitives
and extension hooks; the consumer wires their integrations in their deployed
AgentCore (or Cloud Run) code.

## Quickstart

Install:

```bash
pip install 'emonk[harness-full]'
```

Minimal `app.py`:

```python
from fastapi import FastAPI
from src.core.harness import (
    AgentSpec,
    HarnessConfig,
    IdentitySpec,
    SandboxSpec,
    SecuritySpec,
    build_universal_agent,
)
from src.core.harness.principal import make_user_principal
from src.gateway.harness_routes import router as harness_router
from src.gateway.agentcore_routes import router as agentcore_router

cfg = HarnessConfig(
    agent=AgentSpec(name="enterprise-agent", model="gemini-2.5-pro", provider="google_vertexai"),
    identity=IdentitySpec(dir="./agent_mem", enforce_rules=True),
    security=SecuritySpec(principal_required=True),
    sandbox=SandboxSpec(backend="modal"),
)

compiled = build_universal_agent(cfg)

app = FastAPI()
app.include_router(harness_router)
app.include_router(agentcore_router)
app.state.compiled_agent = compiled
app.state.session_registry = compiled.session_registry
app.state.run_package_writer = compiled.run_package_writer
app.state.approval_channel = compiled.approval_channel
```

Invoke programmatically:

```python
result = await compiled.ainvoke(
    [{"role": "user", "content": "audit my GCP IAM policies"}],
    principal=make_user_principal(user_id="alice", email="alice@corp.com"),
)
print(result["outcome"], result["run_id"])
```

## The seven pillars

| # | Pillar | Components |
|---|---|---|
| 1 | **Identity & Soul** | `IdentitySpec`, `IdentityLoader`, `RulesEnforcementMW`, optional `IdentityResolutionMW` when `identity_source` is configured |
| 2 | **Context Management** | `ContextPolicySpec`, `ContextPolicyMW`, `ToolOutputOffloadMW` |
| 3 | **Sandbox & Permissions** | `SandboxBackend`, `Policy`, `LocalShellSandbox`, `ModalSandbox`, `CommandTierMW` |
| 4 | **Terminal Access** | All sandbox backends implement `execute()` |
| 5 | **Skills & Tools** | `SkillsSpec`, `ToolSpec`, `MCPServerSpec`, `load_mcp_tools` |
| 6 | **Subagent Orchestration** | `SubagentSpec`, `SubagentRecursionMW`, `SubagentResult` |
| 7 | **Evaluation & Observability** | `EventBus`, `HarnessEvent`, `RunPackage`, `SessionRegistry`, control plane HTTP |

## Extension surfaces

These are configured on `HarnessConfig` as discriminated unions (YAML or Python).
The assembler resolves each surface through the registry precedence described in
[`docs/extending-the-harness.md`](extending-the-harness.md).

| Surface | Purpose |
|---|---|
| **Checkpointer** | Conversation / graph checkpoint durability |
| **MemoryStore** | Long-lived agent memory (often LangGraph `BaseStore` compatible) |
| **JobStorage** | Scheduler / job queue persistence |
| **IdentitySource** | Per-invocation identity documents beyond on-disk `IdentitySpec` |
| **SecretResolver** | Resolve secret references for providers and sandboxes |
| **ModelProvider** | Build the `BaseChatModel` from config (Vertex, Bedrock, OpenAI, …) |

## `HarnessConfig` reference

All fields are Pydantic-validated with `extra="forbid"` — unknown keys cause
`ValidationError`. Every sub-spec has sane defaults so minimal configs are
trivial:

```python
cfg = HarnessConfig(agent=AgentSpec(name="x"))
```

The authoritative field list lives in [`src/core/harness/specs.py`](../src/core/harness/specs.py).
Optional extension fields include `checkpointer`, `memory_store`, `job_storage`,
`identity_source`, `secret_resolver`, and `model_provider`.

## CompiledAgent handles

`build_universal_agent(...)` returns a `CompiledAgent` with:

- **`memory_store`**, **`job_storage`**, **`checkpointer_ext`** — resolved ABC-based
  extension instances when configured (see `src/core/harness/compiled_agent.py`).
- **`checkpointer`** — property that prefers `checkpointer_ext` and falls back to
  the legacy protocol checkpointer on `session_registry` for zero-change bots.
- **`identity_source`**, **`secret_resolver`**, **`model_provider`** — optional
  resolved plugin instances.

## Middleware pipeline (frozen order)

The assembler composes middleware in a fixed order. When `HarnessConfig.identity_source`
is set, **`IdentityResolutionMW`** is inserted immediately after
`PrincipalPropagationMW` (position 1); otherwise it is omitted so legacy configs
keep the same stack shape.

Base order (0-based indices before optional insert):

```
0  PrincipalPropagationMW
1  RulesEnforcementMW            ← hard veto from RULES.md
2  RedactionMW(in)
3  ContextPolicyMW                ← budget / summarize / hard-reset
4  SubagentRecursionMW
5  ObservabilityMW
6  CommandTierMW                  ← preapproved / requires_approval / denied
7  HITLApprovalMW                 ← human-in-the-loop approvals
8  ToolOutputOffloadMW
9  RedactionMW(out)
10 RecoveryMW                     ← retry with backoff + synthetic tool messages
11 ObservabilityMW
```

With `identity_source` configured, `IdentityResolutionMW` is inserted at index `1`
and every subsequent index shifts by `+1`.

Consumers can **append** additional middleware via `extra_middleware=[...]` but
cannot reorder the frozen pipeline.

## Framework vs. consumer responsibility

| Concern | Framework (this repo) | Consumer (their repo / AgentCore deploy) |
|---|---|---|
| Seven-pillar enforcement | Yes | — |
| `HarnessConfig` schema | Yes | — |
| Frozen middleware order | Yes | — |
| Six extension surfaces + reference backends | Yes | Credentials, sizing, cost |
| `EventBus` + `HarnessEvent` schema | Yes | — |
| `RunPackage` schema + local / GCS / S3 writers | Yes | — |
| `SandboxBackend` protocol + LocalShell + Modal | Yes | Custom adapters (OpenShell etc.) |
| MCP loader (`load_mcp_tools`) | Yes | MCP server URLs / credentials |
| HITL protocol + Google Chat + webhook channels | Yes | Approval routing / approver identity |
| Control-plane HTTP routes | Yes | Auth middleware, ACLs, IAM integration |
| AgentCore route (`/agentcore/invocations`) | Yes | Deploy to Bedrock AgentCore Runtime |
| LLM provider, model, tools | Default config | **Owned by consumer** |
| Phoenix / DeepEval / OTel wiring | Hooks only | **Owned by consumer** (subscribe event handlers) |

## Running locally

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
python -m src.main
```

Then hit `GET /health`, `GET /harness/introspect`, `POST /agentcore/invocations`.

## Deploying to Bedrock AgentCore Runtime

Your AgentCore-ready Docker image needs:

1. `POST /agentcore/invocations` (SSE on `Accept: text/event-stream`) — already provided by `src/gateway/agentcore_routes.py`.
2. `GET /agentcore/ping` — already provided.
3. A durable **checkpointer** — use the shipped `PostgresCheckpointer` / `MongoCheckpointer`, or implement your own (for example DynamoDB) as a harness extension; see [`examples/extension-dynamodb-checkpointer/`](../examples/extension-dynamodb-checkpointer/).
4. IAM role with `bedrock-agentcore:InvokeAgentRuntime` + whatever access your
   tools need (S3, Secrets Manager, etc.).

Example deploy skeleton:

```python
from fastapi import FastAPI, Depends
from src.core.harness import HarnessConfig, build_universal_agent
from src.gateway.agentcore_routes import router as agentcore_router
from src.gateway.harness_routes import router as harness_router
from my_company.auth import require_bedrock_principal  # injects Principal

cfg = HarnessConfig.from_yaml("harness.yaml")
compiled = build_universal_agent(cfg)

app = FastAPI(dependencies=[Depends(require_bedrock_principal)])
app.include_router(agentcore_router)
app.include_router(harness_router)
app.state.compiled_agent = compiled
app.state.session_registry = compiled.session_registry
app.state.run_package_writer = compiled.run_package_writer
app.state.approval_channel = compiled.approval_channel
```

## Deploying to GCP Cloud Run

Same app; set `GatewaySpec(enable_cloudrun_route=True)`. Mount the gateway's
existing `/health` and `/webhook` endpoints (for Google Chat) alongside
`/harness/*`. Point `ObservabilitySpec.run_package.writer="gcs"` and
`sink_uri="gs://your-bucket/runs/"`.

## Integrating Phoenix, DeepEval, and OTel

Subscribe an event handler. **Handlers are best-effort and isolated** — a
raising or slow handler cannot break the agent.

```python
from src.core.harness import EventBus, EventKind
from phoenix.otel import register

class PhoenixHandler:
    name = "phoenix"

    def __init__(self, tracer):
        self.tracer = tracer

    async def handle(self, event):
        if event.kind == EventKind.LLM_CALL:
            span = self.tracer.start_span("llm.call", attributes=event.payload)
            span.end()

tracer_provider = register(project_name="enterprise-agent")
compiled.event_bus.subscribe(PhoenixHandler(tracer_provider.get_tracer(__name__)))
```

For DeepEval:

```python
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

class DeepEvalHandler:
    name = "deepeval"

    async def handle(self, event):
        if event.kind == EventKind.TASK_COMPLETE:
            pkg_id = event.payload["run_id"]
            # Load RunPackage, run DeepEval, write scores back via another handler or
            # directly into pkg.eval_scores on your own sink.
```

## Identity files

Eight markdown files read from `IdentitySpec.dir`:

- `SOUL.md` — personality, tone, high-level purpose
- `RULES.md` — **hard-enforced deny rules** (see format below)
- `IDENTITY.md` — role
- `USER.md` — user preferences
- `INDEX.md` — memory map
- `MEMORY.md` — long-term memories
- `HEARTBEAT.md` — task queue

`RULES.md` format — one rule per line:

```
- [R-1] DENY_TOOL: git push
- [R-2] DENY_PATTERN: (?i)\bdrop\s+table\b
- [R-3] DENY_SANDBOX_WRITE: /etc/**
```

`DENY_TOOL` uses `fnmatch`, `DENY_PATTERN` uses `re.search`, and
`DENY_SANDBOX_WRITE` uses `fnmatch` against the target path. Soft guidance
(lines without `DENY_`) is forwarded to the LLM unchanged.

## Sandbox backends

Built-in:

- `local_shell` — best-effort, not enterprise-safe (no network isolation)
- `modal` — recommended for production

Custom adapter (e.g. OpenShell):

```python
# mypkg/openshell.py
from src.core.harness.sandbox.protocol import SandboxBackend, SandboxCapabilities

class OpenShellBackend(SandboxBackend):
    name = "openshell"

    def capabilities(self):
        return SandboxCapabilities(
            filesystem_isolation=True,
            network_egress_control=True,
            seccomp=True,
            landlock=True,
            secret_handle_deref=True,
        )

    async def execute(self, cmd, *, policy, cwd=None, stdin=None): ...
    async def read_file(self, path, *, policy): ...
    async def write_file(self, path, content, *, policy): ...
    async def list_files(self, path, *, policy): ...
```

Point `SandboxSpec.backend="custom"` and `custom_import_path="mypkg.openshell:OpenShellBackend"`.
When `SecretResolver` is configured, sandboxes may receive **secret handles**
instead of raw secret material; backends that advertise `secret_handle_deref=True`
must dereference through the resolver.

## MCP integration

```python
cfg = HarnessConfig(
    agent=AgentSpec(name="x"),
    mcp_servers=[
        MCPServerSpec(name="filesystem", command="npx", args=["@modelcontextprotocol/server-filesystem", "/data"]),
        MCPServerSpec(name="internal-docs", transport="http", url="https://mcp.internal.corp/sse"),
    ],
)
```

`load_mcp_tools` (called by the assembler) translates these into LangChain
`BaseTool`s indistinguishable from native tools at the middleware layer.

## RunPackage schema and GRPO readiness

Every invocation writes one immutable JSON document containing:

- Run / session / principal / versions
- All input messages and output messages
- `tool_calls[]` with args (redacted), result summary, tier, approval record, latency
- `token_trace[]` per LLM call
- `subagent_runs[]` (recursive)
- `context_events[]`, `approvals[]`
- `eval_scores: dict[str, float]` (optional, populated by the consumer)
- `outcome ∈ {"pass", "fail", "pass-with-warnings", "escalated"}`

Writers available: `local`, `gcs`, `s3`, `disabled`. Schema is versioned
(`schema_version="1"`) and frozen; downstream GRPO consumers can ingest without
waiting on framework updates.

## Control plane HTTP API

Mounted at `/harness/*` (core control plane):

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/harness/introspect` | Resolved config + middleware names |
| `GET`  | `/harness/introspect/{session_id}` | Session report |
| `POST` | `/harness/control/{session_id}/pause` | Pause |
| `POST` | `/harness/control/{session_id}/resume` | Resume |
| `POST` | `/harness/control/{session_id}/cancel` | Cancel |
| `POST` | `/harness/control/{session_id}/rewind` | Rewind to checkpoint |
| `POST` | `/harness/control/{session_id}/revoke` | Revoke |
| `GET`  | `/harness/control/sessions` | List sessions |
| `GET`  | `/harness/control/approvals/pending` | List pending approvals |
| `POST` | `/harness/control/approvals/{approval_id}/decide` | Resolve approval |
| `GET`  | `/harness/runs/{run_id}` | Fetch RunPackage |
| `GET`  | `/harness/runs` | List RunPackages |

Additional admin routers (mount from `src/gateway/server.py` when harness is enabled):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/harness/identity/bust` | Bust identity cache for a principal (requires `app.state.identity_mw`) |
| `GET`  | `/harness/identity/cache/stats` | Identity cache statistics |
| `GET`  | `/harness/secrets/health` | Composite secret resolver health |
| `GET`  | `/harness/aws/smoke` | AWS stack smoke probes (`{"checks": [...], "all_pass": bool}`; `503` when probes fail) |

**Plugin inventory** is exposed through the CLI (`emonk-harness plugin ls`), not a
separate HTTP listing today.

Auth is the consumer's responsibility — mount FastAPI dependencies as needed.

## CLI: `emonk-harness`

```
emonk-harness lint --config harness.yaml [--strict]
emonk-harness diff old.yaml new.yaml
emonk-harness introspect --config harness.yaml
emonk-harness plugin ls [--strict]
```

Exit codes: `0` clean, `2` warnings, `1` errors.

## Testing strategy

- **Unit** (`tests/harness/`): 50+ tests covering every middleware, spec, and
  protocol in isolation.
- **Functional** (`tests/functional/`): pipeline-level flows using the real
  assembler with a stub LLM; HTTP routes tested via `fastapi.TestClient`.
- **E2E** (`tests/e2e/`): full `build_universal_agent` → FastAPI → invoke →
  RunPackage cycle.

Run all harness tests:

```bash
pytest tests/harness tests/functional tests/e2e -q
```

## Versioning & migration

`HARNESS_SCHEMA_VERSION = "1"`. Older `bot.yaml` configs are automatically
up-migrated by `migrate_config()`; unknown keys land in `extensions`.

Breaking changes bump the major; additive changes ship as v1.x.
