# MonkeyBot

<div align="center">
  <img src="logo.png" alt="MonkeyBot Logo" width="600" />

  <br />

  *Production-ready Python framework for building and deploying LLM agents*

  [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

MonkeyBot (`monkeybot`) is a thin framework for running **tool-using LLM agents** with a **FastAPI SSE gateway**, **SQLite** conversation storage, **MCP** tool servers, and optional skills and memory workflows — wired for local dev and deployable on GCP Cloud Run (or anywhere Docker runs).

> [!NOTE]
> **Not on PyPI yet** — install from a clone of this repository. Skills under `SKILLS_PATH` execute Python code; only add skills from trusted sources.

## Installation

```bash
git clone https://github.com/human-and-machine/monkey-bot.git
cd monkey-bot
uv sync
cd cli && uv sync && uv run monkeybot new --dest .. --yes && cd ..
uv run python -m monkeybot.gateway.main
```

For a self-contained example agent (own `pyproject.toml` + `.venv`, depends on this harness via an editable path — never modifies it), see **[`demo_agent/`](demo_agent/)**:

```bash
cd demo_agent && uv sync
cd ../cli && uv run monkeybot chat --cwd ../demo_agent
```

Full setup (config, `.env`, MCP, Docker): **[Getting Started](docs/getting-started.md)**.

## CLI (`monkeybot`)

The standalone CLI in **[`cli/`](cli/)** scaffolds, validates, and talks to an agent from the terminal. Its one job is **letting you chat with the agent** — `monkeybot chat` starts the gateway for you, reads the port from your `monkeybot.yaml`, connects, and shuts the gateway down on exit.

### Install

```bash
cd cli
uv tool install --editable .   # puts `monkeybot` on your PATH
```

Prefer not to install globally? Use `uv run monkeybot <command>` from inside `cli/` instead.

### Talk to an agent

```bash
cd path/to/agent          # the dir containing monkeybot_config/monkeybot.yaml
monkeybot chat            # spawns the gateway, connects, cleans up on exit
```

Type a message and press Enter. `/bye` exits (and stops the gateway if this command started it). Ctrl-C also exits.

### Agent-first dependencies

The CLI is intentionally thin — it depends only on base `monkeybot` and does **not** pull in provider/storage extras (`bedrock`, `postgres`, …) globally. Those extras are declared on the **agent project** (e.g. `pr-review-agent/pyproject.toml` lists `monkeybot[bedrock,postgres]`). `monkeybot run` and `monkeybot chat` spawn the gateway from the agent project's interpreter so the extras resolve correctly:

1. `<agent>/.venv/bin/python` — used directly when a project venv exists.
2. `uv run python -m monkeybot.gateway.main` — when `<agent>/pyproject.toml` exists but no `.venv`.
3. `sys.executable` (the CLI's interpreter) — legacy fallback for config-only trees (just `monkeybot_config/`, no `pyproject.toml`); in this case extras must be installed in the CLI env.

`monkeybot doctor` checks provider extras and Python version in that same interpreter, and its `remediation` field points at the agent project (`uv sync --extra …` there), not the CLI.

To attach to a gateway you started yourself (e.g. to watch its logs), run it separately and connect with `--attach`:

```bash
monkeybot run            # terminal 1: gateway with live logs
monkeybot chat --attach  # terminal 2: connect to the running gateway
```

### Commands

| Command | Purpose |
|---|---|
| `monkeybot new` | Scaffold `monkeybot_config/`, workspace dirs, and `.env.example` |
| `monkeybot validate` | Check `monkeybot.yaml`, referenced paths, and MCP config (`--json` for machine output) |
| `monkeybot doctor` | Verify Python, provider extras, credentials, and port availability (`--json` for machine output) |
| `monkeybot run` | Start the SSE gateway in the foreground (keeps logs visible) |
| `monkeybot chat` | Talk to the agent; spawns the gateway by default (`--attach` to use a running one) |

Common flags: `--cwd` (agent root, defaults to the current directory), `--config` (explicit `monkeybot.yaml` path), `--port` / `--url` (override the config-derived gateway address). Secrets are read from the agent's `.env`; nothing is committed to `monkeybot.yaml`.

## Agent skill

Install the onboarding skill for Cursor and other agents via [skills.sh](https://skills.sh):

```bash
npx skills add human-plus-machine/monkeybot --skill monkeybot
```

The skill walks through CLI install, scaffolding `monkeybot_config/`, configuration, and your first chat. Source: [`cli/skills/monkeybot/`](cli/skills/monkeybot/).

## Requirements

Python 3.11+ · [uv](https://docs.astral.sh/uv/) · optional provider keys in `.env` (Gemini, OpenAI, Anthropic)

## Key capabilities

### Subagent task queue (optional)

When **`MONKEYBOT_TASK_QUEUE=1`**, the `task` tool enqueues subagent runs via `record_pending` instead of spawning inline. A storage backend (`DB_URL`) is **required** — queue mode without storage raises at enqueue time.

Run workers with `python -m monkeybot.subagents.worker` (production) or `MONKEYBOT_WORKER_POOL=1` on the gateway (development only). Worker tuning:

| Variable | Default | Purpose |
|---|---|---|
| `MONKEYBOT_WORKER_STALE_CLAIM_MS` | `600000` (10 min) | Reclaim `running` rows with no heartbeat after this window; another worker may re-execute the run |
| `MONKEYBOT_WORKER_POLL_INTERVAL_S` | `2` | Poll interval for `pending_runs()` |
| `MONKEYBOT_WORKER_CONCURRENCY` | `1` | Max concurrent claimed runs per worker |
| `MONKEYBOT_WORKER_ID` | auto | Worker identity for claim attribution |

There is no claim heartbeat yet — subagent runs longer than `MONKEYBOT_WORKER_STALE_CLAIM_MS` risk duplicate execution. Increase the limit for long LLM workloads or keep runs under the window.

**Docker:** baseline production-style image — [`docker/Dockerfile`](docker/Dockerfile) + [`docker-compose.yml`](docker-compose.yml). Optional Cloud Run helpers may live in gitignored `internal/` for private forks; see **Step 3** in that doc.
---

**Config:** copy or scaffold **`monkeybot_config/monkeybot.yaml`** from **`monkeybot_config/monkeybot.example.yaml`**. Secrets go in **`.env`** — see the YAML header for variable names.

## Documentation

| Guide | Description |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, configure the gateway, and exercise sessions + SSE from the command line |
| [SSE gateway and custom UI](docs/sse-gateway-ui.md) | HTTP + SSE endpoints, event types, CORS/proxy notes, and how to wire your own frontend |
| [Skills](docs/skills.md) | Skill directory layout and `SKILL.md` discovery |
| [Model Context Protocol](docs/mcp.md) | MCP configuration, env interpolation, OAuth2 flows, and diagnostics |
| [Cloud deployment](docs/cloud-deployment-design.md) | Container and serverless deploy patterns for GCP, AWS, and more |

## Integrations

| Integration | Status | Purpose |
|---|---|---|
| **Google Vertex AI** (Gemini) | Production | Primary LLM provider (gateway + `GeminiProvider`) |
| **Google Cloud Run** | Production | Serverless container hosting |
| **SQLite** | Default (SSE gateway) | Session history and per-turn usage |
| **Google Cloud Firestore** | Supported (`monkeybot[firestore]`) | Session history, threads, and usage via `firestore://` DB_URL |
| **Google Cloud Storage** | Optional (`monkeybot[gcs]`) | Long-term memory and file sync |
| **GCP Secret Manager** | Production | Production secrets management |
| **Google Chat** | Optional | Workspace Add-on interface (when deployed) |
| **OpenAI** | Supported | `OpenAIProvider` (`monkeybot[openai]`) via `get_provider_config()` |
| **Ollama** | Supported | `OllamaProvider` for local models (`monkeybot[ollama]`), no API key required |
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

For provider and cloud wiring, start from **`monkeybot_config/monkeybot.example.yaml`** and the [Cloud deployment](docs/cloud-deployment-design.md) guide.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src/
```

## Support

If you need help or hit a problem, search existing issues or open a new one in the [GitHub issue tracker](https://github.com/human-and-machine/monkey-bot/issues).

## Contributing

Contributions are welcome. You can help by:

- Reporting bugs or gaps in the docs (via issues).
- Suggesting new providers, interfaces, or deployment patterns (feature requests welcome).
- Opening pull requests for fixes and improvements.

Run checks before submitting: `uv run pytest && uv run ruff check . && uv run mypy src/`

## License

You may use, modify, and distribute MonkeyBot under the MIT license. See the [`LICENSE`](LICENSE) file for details.
