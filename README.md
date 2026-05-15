# monkey-bot (monkeybot)

> **The production-ready Python framework for building and deploying LLM agents on cloud infrastructure.**

Built on **native provider adapters** (Gemini via `google-genai`, Anthropic/OpenAI SDKs) behind a small streaming protocol — `monkeybot` handles sessions, tools, and persistence so you can focus on the agent.

---

## What is monkey-bot?

monkey-bot (`monkeybot`) is a thin framework for running **tool-using LLM agents** with a **FastAPI SSE gateway**, **SQLite** conversation and usage storage, **MCP** tool servers, and optional **GCS** / skills / memory-organization workflows — wired for local dev and deployable on GCP Cloud Run (or anywhere Docker runs).

You write **AGENT.md**, tools, and memory layout. `monkeybot` runs the loop, records history, and streams events to clients.

```
Your code               monkeybot framework              Runtime / cloud
─────────────           ─────────────────            ─────────────────
AGENT.md + tools   →    FastAPI SSE gateway    →    Vertex AI / Gemini
SKILL.md + skills  →    Owned agent + loop     →    Optional GCS (extras)
MCP + config       →    SQLite history + usage →    GCP when deployed
```

---

## Features

| Feature | Description |
|---|---|
| **Agent loop** | Streaming provider integration, tool execution, inspectors; SQLite-backed turns |
| **AGENT.md + context** | System prompt from file plus optional memory index and skill list per turn |
| **Persistent memory (optional)** | Markdown under `MEMORY_PATH`, optional GCS extra |
| **Conversation history** | SQLite via `DB_URL` for gateway sessions |
| **Skills** | `SKILL.md` + `run.py` / `main.py` discovery under `SKILLS_PATH` |
| **MCP** | Stdio and streamable HTTP MCP servers; tools exposed as `server__tool` |
| **Memory organizer (optional)** | Async post-processor for classifying and indexing memory files |
| **Multi-provider (library path)** | Vertex Gemini, OpenAI, Anthropic, Vertex Claude via `get_provider_config()` |
| **Zero-config playground** | `playground/agent` + `playground/chat-ui` for local SSE chat |

---

## Documentation (v2)

| Guide | Description |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, configure the SSE gateway, and exercise sessions + SSE from the command line |
| [Skills](docs/skills.md) | Skill directory layout, `SKILL.md`, and `run.py` / `main.py` discovery under `SKILLS_PATH` |

Configuration details for local and cloud runs live in **`.env.example`** at the repository root.

---

## Playground: frontend and backend

The repo includes a minimal **SSE gateway** (Python) and **chat UI** (Vite + React) under `playground/`. Run both for a browser session against your local `AGENT.md` and SQLite.

**1. Backend (gateway)** — from the repository root:

```bash
cd playground/agent
cp .env.example .env   # first time only; edit MODEL_PROVIDER, keys, paths as needed
uv sync                # installs deps including editable monkeybot from repo root
./run.sh
```

The default dev port in `playground/agent/.env.example` is **8787**. Equivalent without the script:

```bash
cd playground/agent && uv run --env-file .env -m monkeybot.gateway.main
```

**2. Frontend (chat UI)** — second terminal, from the repository root:

```bash
cd playground/chat-ui
npm install            # first time only
npm run dev
```

Open the URL Vite prints (usually `http://localhost:5173`). The UI proxies API calls to the gateway via `/__mb_gateway`; by default the dev server targets **`http://127.0.0.1:8787`**. If your gateway runs elsewhere, set `VITE_GATEWAY_TARGET` in `playground/chat-ui/.env.local` (see `env.local.sample`).

More detail: [Getting Started](docs/getting-started.md).

---

## Quick Start

> `monkeybot` is not on PyPI yet — install from source.

### 1. Clone and install

```bash
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot
uv sync
cp .env.example .env
```

`uv sync` installs runtime deps plus the `dev` dependency group (`pytest`, `mypy`, `ruff`).

### 2. Configure

Edit `.env` (the shipped defaults match the bundled example bot):

```bash
MODEL_PROVIDER=google_vertexai
MODEL_NAME=gemini-2.5-flash

DB_URL=sqlite:///data/monkeybot.db
MEMORY_PATH=./data/memory
SKILLS_PATH=./.agents/skills
AGENT_MD=./bots/example-bot/AGENT.md

MCP_CONFIG=./config/mcp.json
COMMAND_TIERS_CONFIG=./config/command_tiers.yaml

# Gemini / Vertex
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
VERTEX_AI_PROJECT_ID=your-gcp-project
```

Full configuration reference: **`.env.example`** at the repository root.

### 3. Use the provider helper (optional)

```python
from monkeybot.core.config import load_secrets, get_provider_config

load_secrets()
# google_vertexai | openai | anthropic | vertex_anthropic — see src/monkeybot/core/config.py
cfg = get_provider_config(provider="google_vertexai", model_name="gemini-2.5-flash")
# cfg.provider.stream(...); cfg.model is the model id string
```

### 4. Run locally

```bash
uv run python -m monkeybot.gateway.main
```

Defaults to port `8000`; set `PORT` (the shipped `.env.example` uses `8080`).

### 5. Deploy to Cloud Run

```bash
./deploy.sh --project your-gcp-project
```

See `deploy.sh --help`-style options at the top of the script. The script builds via Cloud Build using `docker/Dockerfile`, then deploys to Cloud Run.

---

## How It Works

```
┌──────────────────────────────────────────────────────┐
│               Gateway  (FastAPI)                     │
│   GET /health     SSE sessions + reply API           │
└──────────┬─────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────┐
│  Agent loop          │
│  (monkeybot.core.loop)  │
│  ─────────────────── │
│  Provider stream     │
│  Tool calls + exec   │
│  History append      │
└──┬───────────┬───────┘
   │           │
   ▼           ▼
┌───────┐  ┌────────┐
│Skills │  │Memory  │
│       │  │        │
│SKILL.md  │SQLite +│
│+ runner  │markdown │
│          │INDEX   │
└───────┘  └────────┘
```

---

## Project Structure

```
monkey-bot/
├── src/monkeybot/
│   ├── core/                    # Agent loop, context, history, memory, MCP,
│   │   │                        #   subagents, tools, hooks, usage, prompt
│   │   └── providers/gemini.py  # Native Vertex / google-genai adapter
│   ├── gateway/
│   │   ├── main.py              # Uvicorn entry (PORT env)
│   │   └── sse/                 # FastAPI SSE app: app, routes, session_bus
│   ├── providers/               # OpenAI, Anthropic, Vertex-Anthropic adapters
│   └── skills/                  # Skill loader / executor utilities
├── bots/example-bot/            # Reference bot: AGENT.md, MEMORY.md, config.yaml
├── .agents/skills/              # Default SKILLS_PATH (file-ops, memory-search,
│                                #   search-web, self-improve)
├── config/                      # mcp.json, command_tiers.yaml
├── playground/
│   ├── agent/                   # Local gateway runner (.env + run.sh)
│   └── chat-ui/                 # Vite + React dev client
├── examples/skills/             # Sample skills you can copy into .agents/skills
├── docs/                        # getting-started.md, skills.md
├── tests/                       # pytest suite (see pytest.ini)
├── testing/                     # Bench harness (bench.py) + devbot fixture
├── scripts/                     # Operational helpers (e.g. verify_memory.py)
├── docker/Dockerfile            # Container image
├── deploy.sh                    # Cloud Run deploy helper
├── pyproject.toml               # uv / hatchling project config
└── .env.example                 # Canonical env reference
```

---

## Installation

Install from source with `uv`:

```bash
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot
uv sync                           # runtime + dev deps
uv sync --extra gemini            # add a provider extra (gemini already in core deps)
uv sync --extra claude            # Anthropic SDK
uv sync --extra openai            # OpenAI SDK
uv sync --extra vertex-claude     # Anthropic-on-Vertex
uv sync --extra gcs               # Google Cloud Storage backend
```

`monkeybot` is not currently published on PyPI.

---

## Integrations

| Integration | Status | Purpose |
|---|---|---|
| **Google Vertex AI** (Gemini) | Production | Primary LLM provider (gateway + `GeminiProvider`) |
| **Google Cloud Run** | Production | Serverless container hosting |
| **SQLite** | Default (SSE gateway) | Session history and per-turn usage |
| **Google Cloud Storage** | Optional (`monkeybot[gcs]`) | Long-term memory and file sync |
| **GCP Secret Manager** | Production | Production secrets management |
| **Google Chat** | Optional | Workspace Add-on interface (when deployed) |
| **OpenAI** | Supported | `OpenAIProvider` (`monkeybot[openai]`) via `get_provider_config()` |
| **Anthropic Claude** | Supported | `ClaudeProvider` (`monkeybot[claude]`) via `get_provider_config()` |
| **Anthropic via Vertex AI** | Supported | `VertexClaudeProvider` (`anthropic[vertex]`) |
| **AWS Bedrock** | Planned | `providers/bedrock.py` stub; `[bedrock]` extra in `pyproject.toml` |
| **AWS S3** | Planned | Memory store backend |
| **AWS Secrets Manager** | Planned | Secret resolver |
| **Azure OpenAI** | Coming Soon | Azure-hosted OpenAI models |
| **Azure Blob Storage** | Coming Soon | Memory backend for Azure deployments |
| **Azure Key Vault** | Coming Soon | Secrets for Azure deployments |
| **Slack** | Coming Soon | Slack bot interface |
| **Microsoft Teams** | Coming Soon | Teams bot interface |
| **Telegram** | Coming Soon | Telegram bot interface |
| **DynamoDB** | Planned | Checkpointer / job storage |
| **CosmosDB** | Coming Soon | Azure-native persistence options |

For provider and cloud wiring, start from **`.env.example`** and the integration table below; older standalone integration pages were removed with the v2 doc cutover.

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
- **Azure OpenAI** — GPT-4o via Azure-hosted endpoints
- **Groq** — Ultra-low latency inference
- **Ollama** — Local/self-hosted models

### Infrastructure
- **Redis Memory Backend** — High-performance in-memory store option
- **PostgreSQL Scheduler** — Relational job queue for high-throughput scheduling
- **Semantic Memory Search** — Vector embedding search (beyond keyword matching)
- **Multi-Agent Orchestration** — Spawn and coordinate sub-agents from the primary agent

### Developer Experience
- **CLI (`monkeybot new`)** — Scaffold a new bot project in seconds
- **Hot Reload** — Live agent reloading during local development
- **Eval Harness** — Built-in evaluation framework for agent quality testing
- **Dashboard** — Web UI for monitoring agent activity, scheduled jobs, and memory

---

## Security

### Important Considerations

The skills system executes Python code from `SKILLS_PATH` (default `./.agents/skills/`). Only add skills from trusted sources.

**Production Security Checklist:**
- [ ] Review every skill script before adding to `SKILLS_PATH`
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
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot
uv sync                                          # installs dev group automatically

uv run pytest                                    # full test suite
uv run pytest --cov=src/monkeybot --cov-report=html
uv run ruff check .
uv run ruff format .
uv run mypy src/
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Run all checks: `uv run pytest && uv run ruff check . && uv run mypy src/`
4. Commit: `git commit -m "feat: add your feature"`
5. Open a Pull Request

---

## Support

- **Issues**: [GitHub Issues](https://github.com/human-and-machine/monkey-bot/issues)
- **Discussions**: [GitHub Discussions](https://github.com/human-and-machine/monkey-bot/discussions)
- **Reference bot**: See [`bots/example-bot/`](bots/example-bot/) for a complete working example

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

Built with [Vertex AI](https://cloud.google.com/vertex-ai) · [FastAPI](https://fastapi.tiangolo.com)
