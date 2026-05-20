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
| **Skills** | `SKILL.md` discovery under `SKILLS_PATH` (one folder per skill) |
| **MCP** | Stdio and streamable HTTP MCP servers; tools exposed as `server__tool` |
| **Memory organizer (optional)** | Async post-processor for classifying and indexing memory files |
| **Multi-provider (library path)** | Vertex Gemini, OpenAI, Anthropic, Vertex Claude via `get_provider_config()` |
| **Zero-config playground** | `playground/agent` + `playground/chat-ui` for local SSE chat |

---

## Documentation (v2)

| Guide | Description |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, configure the SSE gateway (`monkeybot_config/monkeybot.example.yaml`, optional `.env`), and exercise sessions + SSE from the command line |
| [SSE gateway and custom UI](docs/sse-gateway-ui.md) | v2 HTTP + SSE endpoints, event types, CORS/proxy notes, and the same session flow as `playground/chat-ui` |
| [Skills](docs/skills.md) | Skill directory layout and `SKILL.md` discovery under `SKILLS_PATH` |
| [Model Context Protocol](docs/mcp.md) | MCP configuration, environment variable interpolation, OAuth2 flows, and fail-fast diagnostics |

Harness defaults and comments: **`monkeybot_config/monkeybot.example.yaml`** (copy to `monkeybot.yaml`). Optional **`.env`** in the repo root for secrets — see the header of that YAML file for common variable names.

---

## Playground: frontend and backend

The repo includes a minimal **SSE gateway** (Python) and **chat UI** (Vite + React) under `playground/`. Run both for a browser session against your local prompt and SQLite.

**1. Backend (gateway)** — from the repository root:

```bash
cd playground/agent
uv run monkeybot-init-config --dest .   # optional; or cp monkeybot_config/monkeybot.example.yaml …
uv sync
./run.sh
```

Add **`playground/agent/.env`** when you need secrets (`run.sh` uses `--env-file` only if that file exists). The default dev port in `playground/agent/monkeybot_config/monkeybot.yaml` is **8787**. Without `run.sh`:

```bash
cd playground/agent
uv run -m monkeybot.gateway.main
# or, if you use .env: uv run --env-file .env -m monkeybot.gateway.main
```

Non-secret defaults are also read from `playground/agent/monkeybot_config/monkeybot.yaml` when environment variables are unset (after `.env` is loaded).

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

> **`monkeybot` is not on PyPI yet** — install from a clone of this repository (`uv` or `pip`).

### 1. Clone and install dependencies

```bash
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot
uv sync
uv run monkeybot-init-config   # optional: scaffold monkeybot_config/ + data/memory + .agents/skills
```

Equivalent with **pip** (from the repo root, with extras as needed):

```bash
pip install -e .
# e.g. pip install -e ".[gemini,openai,claude]"
```

`uv sync` also pulls the `dev` group (`pytest`, `mypy`, `ruff`).

### 2. After installation — wire the harness

Do the following from the directory that will be your **gateway process working directory** (usually the repo root). Relative paths in YAML resolve against that cwd unless you use absolute paths or set **`MONKEYBOT_CONFIG`** to a specific `monkeybot.yaml` file.

| Step | What to do |
|------|----------------|
| **0. Quick scaffold (optional)** | From the repo root: **`uv run monkeybot-init-config`** (same as **`uv run python -m monkeybot.cli.init_config`**). Creates **`monkeybot_config/`** with **`monkeybot.yaml`**, **`monkeybot.example.yaml`**, **`mcp.json`**, **`command_allowlist.yaml`**, **`AGENT.md`**, and ensures **`data/memory/`** (with a starter **`INDEX.md`**) and **`.agents/skills/`**. Existing files are left untouched; use **`--force`** to overwrite. Use **`--dest PATH`** to scaffold somewhere other than the current directory (e.g. **`playground/agent`**). |
| **1. Config file** | If you skipped step 0: **`cp monkeybot_config/monkeybot.example.yaml monkeybot_config/monkeybot.yaml`**, then edit **`monkeybot_config/monkeybot.yaml`**. All non-secret tuning lives here (`paths.*`, `model.*`, gateway, curation, web search, sandbox, …). |
| **2. Secrets (optional)** | Create a **`.env`** in the same cwd only when you need API keys, `GOOGLE_APPLICATION_CREDENTIALS`, or other overrides. Variable names are listed in the header of **`monkeybot_config/monkeybot.example.yaml`**. If you use `model.provider: fake`, you can skip `.env` until you call a real provider. |
| **3. Prompt** | Ensure the file at **`paths.agent_md`** exists (the repo defaults to **`monkeybot_config/AGENT.md`**). |
| **4. MCP / policy (optional)** | Adjust **`monkeybot_config/mcp.json`** and **`command_allowlist.yaml`** if you use them; paths are set under `paths` in YAML (`command_allowlist_config`). |
| **5. Run** | `uv run python -m monkeybot.gateway.main` (or `python -m monkeybot.gateway.main` from an activated venv). |
| **6. Playground UI (optional)** | Use the **Playground** section below (`playground/agent` + `playground/chat-ui`). |

Full commented template: **`monkeybot_config/monkeybot.example.yaml`**. Step-by-step API checks: **[Getting Started](docs/getting-started.md)**.

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

The gateway listens on **`runtime.port`** from `monkeybot_config/monkeybot.yaml` (the shipped file uses **8080**); if unset, the process falls back to **`PORT` / `GATEWAY_PORT`**, then **8000**.

### 5. Run with Docker (optional)

```bash
cp .env.example .env   # add GEMINI_API_KEY at minimum
docker compose up --build
# gateway listening at http://localhost:8080
```

With OpenSandbox as a sidecar (isolated code execution inside the agent):

```bash
docker compose -f docker-compose.yml -f docker/docker-compose.sandbox.yml up --build
```

The sandbox overlay mounts the agent workspace at `/tmp/monkeybot-workspace` — a path visible to both the container and the host Docker daemon (required for OpenSandbox bind-mounts). The default `docker/opensandbox.docker.toml` restricts sandbox containers to `/tmp`; to allow broader host mounts (e.g. your repo under `$HOME`) set `OPENSANDBOX_CONFIG=./path/to/your/opensandbox.toml` in `.env`.

Managed deploy (Cloud Run, ECS, etc.): see **[Cloud deployment design](docs/cloud-deployment-design.md)** (Step 4 guides when added). Build arg **`EXTRAS`** selects pip extras in `docker/Dockerfile` (same image for laptop and cloud).

**Playground Docker:** local smoke test with harness + workspace paths — `docker-compose.playground.yml` + [`docker/Dockerfile.playground`](docker/Dockerfile.playground). Optional Cloud Run helpers may live in gitignored `internal/` for private forks; see **Step 3** in that doc.
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
│  (monkeybot.core.runtime.loop)  │
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
│   │   │                        #   llm (protocol + usage), subagents, tools, hooks, prompt
│   ├── gateway/
│   │   ├── main.py              # Uvicorn entry (PORT env)
│   │   └── sse/                 # FastAPI SSE app: app, routes, session_bus
│   ├── cli/                     # init_config scaffold (monkeybot-init-config)
│   ├── providers/               # LLM adapters: Gemini (Vertex), OpenAI, Anthropic, …
│   └── skills/                  # Skill loader / executor utilities
├── bots/example-bot/            # Reference bot: MEMORY.md, config.yaml
├── monkeybot_config/            # monkeybot.yaml, monkeybot.example.yaml, AGENT.md, mcp.json, …
├── .agents/skills/              # Default SKILLS_PATH (file-ops, memory-search,
│                                #   search-web, self-improve)
├── playground/
│   ├── agent/                   # Local gateway runner (.env + run.sh)
│   └── chat-ui/                 # Vite + React dev client
├── examples/skills/             # Sample skills you can copy into .agents/skills
├── docs/                        # getting-started.md, skills.md, mcp.md
├── tests/                       # pytest suite (see pytest.ini)
├── testing/                     # Bench harness (bench.py) + devbot fixture
├── docker/
│   ├── Dockerfile               # Container image (Pattern A baseline)
│   ├── docker-compose.sandbox.yml  # Compose overlay (+ OpenSandbox sidecar)
│   └── opensandbox.docker.toml     # Default OpenSandbox server config (compose)
├── docker-compose.yml           # Local baseline: monkeybot + ./data volume
├── .env.example                 # Env template for Docker / local secrets
├── scripts/                     # Operational helpers (e.g. verify_memory.py)
├── pyproject.toml               # uv / hatchling project config
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
| **AWS Bedrock** | Supported | `BedrockClaudeProvider` (`monkeybot[bedrock]`); `MODEL_PROVIDER=aws_bedrock` |
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

For provider and cloud wiring, start from **`monkeybot_config/monkeybot.example.yaml`** and the integration table below; older standalone integration pages were removed with the v2 doc cutover.

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
- **Reference bot**: See [`bots/example-bot/`](bots/example-bot/) for example memory layout; the default system prompt lives in [`monkeybot_config/AGENT.md`](monkeybot_config/AGENT.md).

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

Built with [Vertex AI](https://cloud.google.com/vertex-ai) · [FastAPI](https://fastapi.tiangolo.com)
