# monkey-bot (emonk)

> **The production-ready Python framework for building and deploying LLM agents on cloud infrastructure.**

Built on [LangChain](https://github.com/langchain-ai/langchain) and [LangGraph](https://github.com/langchain-ai/langgraph) — monkey-bot handles the infrastructure so you can focus on building your agent.

---

## What is monkey-bot?

monkey-bot (`emonk`) is a thin, opinionated framework that takes a LangGraph agent from zero to production in minutes. It provides everything below the agent logic layer — cloud storage, memory persistence, conversation checkpointing, a cron scheduler, a skills engine, voice support, and a Google Chat gateway — all wired together and ready to deploy on GCP Cloud Run (or AWS, or anywhere Docker runs).

You write the system prompt, tools, and job handlers. monkey-bot handles the rest.

```
Your Code               monkey-bot Framework        Cloud Infrastructure
─────────────           ────────────────────         ────────────────────
system_prompt.md   →    3-layer prompt engine   →    Vertex AI / Gemini
tools/skills       →    skills engine           →    GCS memory bucket
job handlers       →    cron scheduler          →    Cloud Scheduler
bot.yaml           →    config loader           →    GCP Secret Manager
                        FastAPI gateway         →    Cloud Run
                        PII filtering           →    Google Chat
                        Firestore checkpoints   →    Firestore
```

---

## Features

| Feature | Description |
|---|---|
| **Deep Agent Core** | Built on `create_deep_agent` with full LangGraph state management |
| **3-Layer Prompt System** | SOUL, IDENTITY, USER, and INDEX files compose the agent's context automatically |
| **Persistent Memory** | GCS-backed LangGraph Store with keyword search and session summaries |
| **Conversation Checkpoints** | InMemory (dev) or Firestore (production) — survives restarts |
| **Cron Scheduler** | Cloud Scheduler-driven background jobs with JSON or Firestore storage, distributed locking |
| **Skills Engine** | Add capabilities via `SKILL.md` + Python entry points — no framework changes needed |
| **Google Chat Gateway** | Full Workspace Add-on support with PII filtering and allowlist auth |
| **Voice Support** | GCP Speech-to-Text input and Text-to-Speech output (optional) |
| **LLM Council** | Async post-processor that classifies and indexes raw memory files |
| **Heartbeat System** | Scheduled agent self-checks with configurable Google Chat reporting |
| **GCS Filesystem Sync** | Bidirectional memory sync between container and GCS on every startup/shutdown |
| **Multi-Provider LLMs** | Google Vertex AI, OpenAI, Anthropic — switch with one config line |
| **Zero-Config Defaults** | Works out of the box locally; production settings added incrementally |

---

## Documentation

| Guide | Description |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, configure, and run your first bot in 5 minutes |
| [Creating an Agent](docs/creating-an-agent.md) | Step-by-step guide to building a production bot with `bot.yaml` + `build_deep_agent` |
| [Creating a Harness Agent](docs/creating-a-harness-agent.md) | End-to-end `HarnessConfig` + `build_universal_agent` + FastAPI wiring |
| [Agent Harness](docs/agent-harness.md) | Seven pillars + six extension surfaces: `HarnessConfig`, middleware, sandbox, RunPackage, control plane, AgentCore/Cloud Run deploy, Phoenix/DeepEval hooks |
| [Prompt & Identity Guide](docs/prompt-and-identity-guide.md) | SOUL, IDENTITY, USER, INDEX, HEARTBEAT — what each file is and when the agent uses it |
| [Deploy to GCP](docs/deploy-gcp.md) | Deploy to Google Cloud Run with full infrastructure setup |
| [Deploy to AWS](docs/deploy-aws.md) | AWS deployment guide *(coming soon)* |
| [Configuration Reference](docs/configuration.md) | Every `bot.yaml` option and environment variable |
| [Memory System](docs/memory.md) | How GCS memory, session summaries, and the LLM Council work |
| [Scheduler & Jobs](docs/scheduler.md) | Background jobs, cron expressions, and job handlers |
| [Skills System](docs/skills.md) | Building and registering agent skills |
| [Voice](docs/voice.md) | STT/TTS voice integration with GCP |
| [Integrations](docs/integrations.md) | All supported integrations and configuration |

---

## Quick Start

### 1. Install

```bash
pip install emonk
```

Or clone the reference implementation:

```bash
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot/test-monkey
cp .env.example .env
```

### 2. Configure

Edit `bot.yaml` (non-secret config):

```yaml
agent:
  name: my-bot
  skills_dir: ./skills

model:
  provider: google_vertexai
  name: gemini-2.5-flash
  temperature: 0.7

gateway:
  allowed_users:
    - you@yourcompany.com
```

Edit `.env` (secrets — never committed):

```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
VERTEX_AI_PROJECT_ID=your-gcp-project
```

### 3. Write your agent

```python
# src/main.py
from emonk.core.config import load_secrets, get_model
from emonk.core.deepagent import build_deep_agent
from emonk.core.scheduler import CronScheduler
from langchain_core.tools import tool

load_secrets()

@tool
def say_hello(name: str) -> str:
    """Say hello to someone."""
    return f"Hello, {name}!"

model = get_model()
scheduler = CronScheduler()
agent = build_deep_agent(
    model=model,
    tools=[say_hello],
    user_system_prompt="You are a friendly assistant.",
    scheduler=scheduler,
)
```

### 4. Run locally

```bash
python -m src.main
```

### 5. Deploy

```bash
./deploy.sh
```

Done. Your agent is live on Cloud Run.

---

## How It Works

```
┌──────────────────────────────────────────────────────┐
│               Gateway  (FastAPI)                     │
│   POST /webhook    POST /voice    POST /cron/tick    │
│   GET  /health     GET  /         POST /run          │
└──────────┬──────────────────────────────┬────────────┘
           │                              │
           ▼                              ▼
┌──────────────────────┐       ┌─────────────────────┐
│  Agent Core          │       │  Cloud Scheduler     │
│  (LangGraph)         │       │  POST /cron/tick     │
│  ─────────────────── │       │  every minute        │
│  3-Layer Prompt      │       └─────────┬───────────┘
│  Tool Calling        │                 │
│  State Management    │                 ▼
│  Checkpointing       │       ┌─────────────────────┐
└──┬───────────┬───────┘       │  CronScheduler      │
   │           │               │  JSON / Firestore   │
   ▼           ▼               │  Distributed Lock   │
┌───────┐  ┌────────┐          └─────────────────────┘
│Skills │  │Memory  │
│Engine │  │Manager │
│       │  │        │
│SKILL.md  │GCSStore│
│+ Python  │Session │
│ tools    │Summaries
└───────┘  │Index   │
           │Council │
           └────────┘
```

---

## Project Structure

```
monkey-bot/
├── src/
│   ├── core/
│   │   ├── deepagent.py          # build_deep_agent() — primary public API
│   │   ├── agent.py              # build_agent() — legacy API
│   │   ├── config.py             # Config loading, secrets, model factory
│   │   ├── prompt.py             # 3-layer system prompt composition
│   │   ├── council.py            # LLM Council — async memory post-processor
│   │   ├── store.py              # GCSStore + search_memory tool
│   │   ├── filesystem_sync.py    # GCS ↔ local memory sync
│   │   ├── firestore_checkpointer.py  # Firestore conversation persistence
│   │   ├── middleware.py         # SessionSummaryMiddleware
│   │   ├── terminal.py           # Sandboxed subprocess runner
│   │   └── scheduler/
│   │       ├── cron.py           # CronScheduler
│   │       ├── storage.py        # JSONFileStorage + FirestoreStorage
│   │       └── handlers.py       # HeartbeatHandler
│   ├── gateway/
│   │   ├── server.py             # FastAPI app + all endpoints
│   │   ├── models.py             # Request/response Pydantic models
│   │   ├── pii_filter.py         # Email hashing, metadata stripping
│   │   └── interfaces.py         # AgentCoreInterface ABC
│   ├── skills/
│   │   ├── loader.py             # SkillLoader
│   │   └── executor.py           # SkillsEngine
│   ├── backends/
│   │   └── gcs.py                # GCS backend
│   ├── sandbox/
│   │   └── modal.py              # Modal.com sandbox
│   └── voice/
│       └── handler.py            # GCP STT/TTS
├── skills/                        # Built-in skills
│   ├── file-ops/
│   ├── memory/
│   └── search-web/
├── docs/                          # Full documentation
├── tests/
├── bot.yaml                       # Reference bot config
├── .env.example                   # Environment variable template
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

---

## Installation

```bash
# Core framework
pip install emonk

# With Google Cloud Storage
pip install "emonk[gcs]"

# With voice (STT/TTS)
pip install "emonk[voice]"

# With Modal sandbox execution
pip install "emonk[modal]"

# Everything
pip install "emonk[all]"
```

**Or install the development version directly from source:**

```bash
pip install git+https://github.com/human-and-machine/monkey-bot.git@main
```

---

## Integrations

| Integration | Status | Purpose |
|---|---|---|
| **Google Vertex AI** (Gemini) | Production | Primary LLM provider |
| **Google Cloud Run** | Production | Serverless container hosting |
| **Google Cloud Storage** | Production | Long-term memory and file sync |
| **Google Cloud Firestore** | Production | Conversation checkpoints and distributed job locks |
| **Google Cloud Scheduler** | Production | Cron job triggers |
| **GCP Secret Manager** | Production | Production secrets management |
| **Google Chat** | Production | Workspace Add-on interface |
| **GCP Speech-to-Text** | Production | Voice input |
| **GCP Text-to-Speech** | Production | Voice output |
| **OpenAI** | Production | Alternative LLM provider |
| **Anthropic Claude** | Production | Alternative LLM provider |
| **Anthropic via Vertex AI** | Production | Claude hosted on Google infrastructure |
| **LangChain / LangGraph** | Production | Agent orchestration and state |
| **Modal.com** | Beta | Sandboxed code execution |
| **AWS Bedrock** | Production | Model provider via Agent Harness (`ModelProvider` / `BedrockProvider`) |
| **AWS S3** | Production | Memory store backend via Agent Harness |
| **AWS Secrets Manager** | Production | Secret resolver via Agent Harness |
| **Azure OpenAI** | Coming Soon | Azure-hosted OpenAI models |
| **Azure Blob Storage** | Coming Soon | Memory backend for Azure deployments |
| **Azure Key Vault** | Coming Soon | Secrets for Azure deployments |
| **Slack** | Coming Soon | Slack bot interface |
| **Microsoft Teams** | Coming Soon | Teams bot interface |
| **Telegram** | Coming Soon | Telegram bot interface |
| **DynamoDB** | Extension | Checkpointer / job storage via custom harness plugins (see `examples/extension-dynamodb-checkpointer/`) |
| **CosmosDB** | Coming Soon | Azure scheduler job storage |

See [integrations docs](docs/integrations.md) for configuration details on all active integrations.

---

<!-- BEGIN harness-extensibility story 9 -->
## Extending the Harness

monkey-bot's harness ships 32 reference backends across six extension pillars
(Checkpointer, MemoryStore, JobStorage, IdentitySource, SecretResolver,
ModelProvider) and treats every unshipped backend — DynamoDB, Redis, Vault,
Pinecone, Azure Key Vault, etc. — as a **first-class extension target**, not a
dead end. A new backend is a ~80-line subclass of the relevant ABC plus one of
three registration mechanisms (programmatic, `import_path` in YAML, or
pip-installed entry point).

| Guide | What it covers |
|---|---|
| [Extending the Harness — master guide](docs/extending-the-harness.md) | Registry precedence, the three extension mechanisms, worked Redis + DynamoDB examples, contract-test hookup, CI wiring, supply-chain hygiene |
| [DynamoDB Checkpointer example](examples/extension-dynamodb-checkpointer/) | Canonical pip-installable plugin — ~100 LOC, ships the `emonk.checkpointers` entry point, runs the framework contract suite via `moto` |
| [Backend matrix](docs/harness/backend-matrix.md) | Shipped vs. non-shipped grid and how to reach every row via extension |
| [Identity sources](docs/harness/identity-source.md) | Per-invocation lifecycle, cache semantics, `POST /harness/identity/bust` walk-through |
| [Secret resolvers](docs/harness/secret-resolver.md) | Composite chains + AWS/GCP rotation playbooks |
| [Model providers](docs/harness/model-provider.md) | Bedrock, OpenAI, Anthropic, Vertex, Ollama wiring recipes |
| [AWS enterprise runbook](docs/harness/aws-enterprise-runbook.md) | ≤ 30-minute deployment runbook (Bedrock + Postgres + S3 + Secrets Manager stack) |
| [Postgres backends](docs/harness/postgres-backends.md) | DDL listings, pool sizing table, Alembic opt-in |
| [Mongo backends](docs/harness/mongo-backends.md) | Replica-set guidance + non-RS fallback caveats |
| [Plugin operations](docs/harness/plugin-operations.md) | `plugin ls`, collision resolution, supply-chain posture |

The [`Dockerfile.extension-template`](Dockerfile.extension-template) at the repo
root ships `--require-hashes` and `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1`
out of the box — copy it into any consumer repo that bundles an extension
package.
<!-- END harness-extensibility story 9 -->

---

## Coming Soon

### Platform Support
- **AWS Deployment** — Full ECS/Fargate and Lambda deployment guides with CDK infrastructure templates
- **Azure Deployment** — Azure Container Apps deployment with Bicep templates
- **Kubernetes** — Helm charts for self-hosted deployments

### New Interfaces
- **Slack Bot** — Direct message and channel bot with slash commands
- **Microsoft Teams** — Teams app integration with adaptive cards
- **Telegram** — Telegram bot with inline keyboard support
- **REST API Mode** — Headless operation for programmatic access

### LLM Providers
- **AWS Bedrock** — Native Bedrock integration (Claude, Llama, Titan)
- **Azure OpenAI** — GPT-4o via Azure-hosted endpoints
- **Groq** — Ultra-low latency inference
- **Ollama** — Local/self-hosted models

### Infrastructure
- **Redis Memory Backend** — High-performance in-memory store option
- **PostgreSQL Scheduler** — Relational job queue for high-throughput scheduling
- **Semantic Memory Search** — Vector embedding search (beyond keyword matching)
- **Multi-Agent Orchestration** — Spawn and coordinate sub-agents from the primary agent

### Developer Experience
- **CLI (`emonk new`)** — Scaffold a new bot project in seconds
- **Hot Reload** — Live agent reloading during local development
- **Eval Harness** — Built-in evaluation framework for agent quality testing
- **Dashboard** — Web UI for monitoring agent activity, scheduled jobs, and memory

---

## Security

### Important Considerations

The skills system executes Python code from the `./skills/` directory. Only add skills from trusted sources.

**Production Security Checklist:**
- [ ] Review every skill script before adding to `./skills/`
- [ ] Use separate GCP projects for dev/production
- [ ] Limit service account permissions to minimum required (principle of least privilege)
- [ ] Store all secrets in GCP Secret Manager — never in `bot.yaml` or environment files
- [ ] Set `ALLOWED_USERS` to restrict who can interact with the bot
- [ ] Enable GCS bucket encryption at rest
- [ ] Rotate service account keys every 90 days
- [ ] Monitor Cloud Run logs for unexpected behavior

---

## Development

```bash
# Clone and install with dev dependencies
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=src --cov-report=html

# Lint
ruff check .

# Format
ruff format .

# Type check
mypy src/
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Run all checks: `pytest && ruff check . && mypy src/`
4. Commit: `git commit -m "feat: add your feature"`
5. Open a Pull Request

---

## Support

- **Issues**: [GitHub Issues](https://github.com/human-and-machine/monkey-bot/issues)
- **Discussions**: [GitHub Discussions](https://github.com/human-and-machine/monkey-bot/discussions)
- **Reference bot**: See [`test-monkey/`](test-monkey/) for a complete working example

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

Built with [LangChain](https://github.com/langchain-ai/langchain) · [LangGraph](https://github.com/langchain-ai/langgraph) · [Vertex AI](https://cloud.google.com/vertex-ai) · [FastAPI](https://fastapi.tiangolo.com)
