# monkeybot

<div align="center">
  <img src="logo.png" alt="monkeybot Logo" width="600" />

  <br />

  *Multi-cloud agent harness — GCP-first docs, portable runtime*

  [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

monkeybot is a thin **multi-cloud-capable** harness for tool-using LLM agents: FastAPI SSE gateway, pluggable storage (SQLite/Postgres/Firestore), MCP tools, skills, and memory. It runs locally with zero cloud dependencies and ships the same container image to any Docker host.

> [!TIP]
> **Positioning:** monkeybot is an owned agent runtime with a focused provider set — not a universal integration catalog. **Docs and examples are GCP-first** (Vertex, GCS, Cloud Run, Agent Engine); **AWS** paths (Bedrock, S3, AgentCore, ECS/Lambda) are shipped in the Pattern guides; Azure coverage is thinner. See [Cloud deployment — Positioning](docs/cloud-deployment-design.md#positioning).

> [!NOTE]
> Skills under `SKILLS_PATH` execute Python code; only add skills from trusted sources.

## Installation

Install [uv](https://docs.astral.sh/uv/), then the global CLI (no repo clone required):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if needed
uv tool install monkeybot-cli

monkeybot new --dest ./my-agent --provider openai --yes
cd my-agent
uv sync
cp .env.example .env   # add provider keys
monkeybot doctor
monkeybot chat
```

`monkeybot new` writes an agent `pyproject.toml` with `monkeybot[<provider>]` (plus any `--with` extras). Plain `uv sync` installs the harness into the agent `.venv`. Upgrade the CLI with `uv tool upgrade monkeybot-cli`.

Full walkthrough (config, `.env`, MCP, Docker): **[Getting Started](docs/getting-started.md)**.

### Developing the harness (contributors)

Clone only if you are changing monkeybot itself:

```bash
git clone https://github.com/human-plus-machine/monkeybot.git
cd monkeybot && uv sync
cd cli && uv sync
uv tool install --editable .
```

For a self-contained example agent that depends on this checkout via an editable path, see **[`demo_agent/`](demo_agent/)**.

## CLI (`monkeybot`)

The CLI scaffolds, validates, and talks to an agent from the terminal. Use `monkeybot chat` for the turn-based SSE gateway, or `monkeybot talk` for the realtime WebSocket gateway (audio/text).

### Talk to an agent

```bash
cd path/to/agent          # the dir containing monkeybot_config/monkeybot.yaml
monkeybot chat            # spawns the gateway, connects, cleans up on exit
```

Type a message and press Enter. `/bye` exits (and stops the gateway if this command started it). Ctrl-C also exits.

### Agent-first dependencies

The CLI is intentionally thin — it does **not** pull in provider/storage extras (`bedrock`, `postgres`, …) globally. Those extras are declared on the **agent project** (scaffolded `pyproject.toml` lists `monkeybot[…]`). `monkeybot run` and `monkeybot chat` spawn the gateway from the agent project's interpreter so the extras resolve correctly:

1. `<agent>/.venv/bin/python` — used directly when a project venv exists.
2. `uv run python -m monkeybot.gateway.main` — when `<agent>/pyproject.toml` exists but no `.venv`.
3. `sys.executable` (the CLI's interpreter) — legacy fallback for config-only trees (just `monkeybot_config/`, no `pyproject.toml`); in this case extras must be installed in the CLI env.

`monkeybot doctor` checks provider extras and Python version in that same interpreter, and its `remediation` field points at the agent project (add `monkeybot[<extra>]` to `pyproject.toml` dependencies, then `uv sync`), not the CLI.

To attach to a gateway you started yourself (e.g. to watch its logs), run it separately and connect with `--attach`:

```bash
monkeybot run            # terminal 1: gateway with live logs
monkeybot chat --attach  # terminal 2: connect to the running gateway
```

### Commands

| Command | Purpose |
|---|---|
| `monkeybot new` | Scaffold `monkeybot_config/`, workspace dirs, agent `pyproject.toml`, and `.env.example` |
| `monkeybot validate` | Check `monkeybot.yaml`, referenced paths, and MCP config (`--json` for machine output) |
| `monkeybot doctor` | Verify Python, provider extras, credentials, and port availability (`--json` for machine output) |
| `monkeybot run` | Start the SSE gateway in the foreground (keeps logs visible) |
| `monkeybot chat` | Talk to the agent (SSE); spawns the gateway by default (`--attach` to use a running one) |
| `monkeybot talk` | Realtime voice/text client (WebSocket); audio by default, `--text` for typed input |

Common flags: `--cwd` (agent root, defaults to the current directory), `--config` (explicit `monkeybot.yaml` path), `--port` / `--url` (override the config-derived gateway address). Secrets are read from the agent's `.env`; nothing is committed to `monkeybot.yaml`.

Realtime audio (`monkeybot talk` without `--text`) needs PortAudio plus `monkeybot[cli-realtime]` in the agent env (already included for `demo_agent`).

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

**Config:** copy or scaffold **`monkeybot_config/monkeybot.yaml`** from **`monkeybot_config_example/monkeybot.example.yaml`** (or the packaged scaffold template). Secrets go in **`.env`** — see the YAML header for variable names.

## Documentation

| Guide | Description |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, configure the gateway, and exercise sessions + SSE from the command line |
| [SSE gateway and custom UI](docs/sse-gateway-ui.md) | HTTP + SSE endpoints, event types, CORS/proxy notes, and how to wire your own frontend |
| [Skills](docs/skills.md) | Skill directory layout and `SKILL.md` discovery |
| [Model Context Protocol](docs/mcp.md) | MCP configuration, env interpolation, OAuth2 flows, and diagnostics |
| [Cloud deployment](docs/cloud-deployment-design.md) | Multi-cloud Patterns A/B/C — **GCP-first guides**, AWS addenda, portable harness |

## Integrations

Tables below reflect what **shipped in v2.0.0** (see [CHANGELOG](CHANGELOG.md)). Longer-term platform work lives in [BACKLOG.md](BACKLOG.md).

### LLM providers

| Provider | Status | Install extra |
|---|---|---|
| **Google Vertex AI / Gemini** | Supported | `monkeybot[gemini]` or ADC |
| **OpenAI** | Supported | `monkeybot[openai]` |
| **Anthropic Claude** | Supported | `monkeybot[claude]` |
| **Anthropic on Vertex AI** | Supported | `monkeybot[vertex-claude]` |
| **AWS Bedrock** | Supported | `monkeybot[bedrock]` |
| **HuggingFace Inference** | Supported | `monkeybot[huggingface]` |
| **Ollama** | Supported | `monkeybot[ollama]` |
| **NVIDIA NIM** (build.nvidia.com) | Supported | `monkeybot[nvidia]` |

### Storage & persistence

| Backend | Status | Install extra |
|---|---|---|
| **SQLite** | Default | — (SSE gateway session history) |
| **Postgres** | Supported | `monkeybot[postgres]` |
| **Google Cloud Firestore** | Supported | `monkeybot[firestore]` |
| **Local filesystem** | Default | `local://` memory URI |
| **Google Cloud Storage** | Supported | `monkeybot[gcs]` |
| **AWS S3** | Supported | `monkeybot[aws]` |

### Platform & tooling

| Integration | Status | Notes |
|---|---|---|
| **FastAPI SSE gateway** | Shipped | Sessions, streaming, health |
| **MCP** (stdio + HTTP) | Shipped | `monkeybot_config/mcp.json` |
| **Browser MCP** | Shipped | `integrations/browser-mcp/` |
| **OpenSandbox** | Shipped | Optional sandbox (`monkeybot[sandbox]`) |
| **Web search** | Shipped | DuckDuckGo (default, `monkeybot[web-search]`); Tavily / Firecrawl / Vertex grounding |
| **OpenTelemetry** | Shipped | `monkeybot[observability]` |
| **Docker / Cloud Run** | Shipped | Pattern A — [deploy guide](docs/deploy-pattern-a-container.md) |
| **Pattern C adapters** | Shipped | Agent platforms — e.g. AWS Bedrock AgentCore (`examples/agentcore/`), Vertex AI Agent Engine |

### Roadmap (near-term)

| Item | Notes |
|---|---|
| **Slack gateway** | Events API / socket mode — [BACKLOG](BACKLOG.md#1-chat-integration) |
| **Google Chat gateway** | Workspace webhook handler — [BACKLOG](BACKLOG.md#1-chat-integration) |
| **Harness evals** | Langfuse / deepeval traceability in the agent loop — in progress |
| **Scheduler in gateway** | Cron jobs for long-running deployments — design in [BACKLOG](BACKLOG.md#3-scheduler) |

Azure, Microsoft Teams, Telegram, DynamoDB, CosmosDB, and managed secret resolvers (AWS Secrets Manager, Azure Key Vault, full GCP Secret Manager wiring) are tracked under **Future platforms** in [BACKLOG.md](BACKLOG.md).

For provider and cloud wiring, start from **`monkeybot_config_example/monkeybot.example.yaml`** and the [Cloud deployment](docs/cloud-deployment-design.md) guide.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src/
```

## Support

If you need help or hit a problem, search existing issues or open a new one in the [GitHub issue tracker](https://github.com/human-plus-machine/monkeybot/issues).

## Contributing

Contributions are welcome. You can help by:

- Reporting bugs or gaps in the docs (via issues).
- Suggesting new providers, interfaces, or deployment patterns (feature requests welcome).
- Opening pull requests for fixes and improvements.

Run checks before submitting: `uv run pytest && uv run ruff check . && uv run mypy src/`

### Releasing

`develop` is the working branch; `main` always reflects the latest release.

1. Anyone runs the **Prepare release** workflow (Actions tab → Prepare release → Run workflow), choosing `core` or `cli` and a version bump. It bumps the package's `pyproject.toml`, moves the `CHANGELOG.md` `Unreleased` section into a dated entry on a `release/<package>-v<version>` branch, and opens a PR from that branch into `main` (`develop` itself is untouched until the PR merges, so an abandoned release PR leaves no trace).
2. An admin reviews and **merges the PR** (branch protection on `main` restricts who can merge — see below). Use a regular merge commit, not squash, so `main`'s history and the release tag line up with what was reviewed.
3. The **Publish release** workflow runs automatically on that merge: it tags the new version, creates a GitHub Release from the changelog entry, Trusted-Publishes the released package(s) to PyPI (core before CLI when both ship), and merges `main` back into `develop` so the two branches don't drift apart.

One-time setup (repo Settings → Branches → add rule for `main`): require a pull request before merging, and restrict who can push to matching branches to Admins. That's the only access control needed — anyone can prepare a release, only admins can promote it to `main`.

**Before relying on this:** `develop` and `main` currently have diverged history (independent commits on each side). Reconcile them once with a manual merge before running the first automated release, or the first release PR may show unrelated changes or conflicts. `CHANGELOG.md` is shared across both packages — `core` and `cli` versions are bumped independently but their release notes live in the same file. The current versions already on `main` (`core` 2.0.0, `cli` 0.1.7) predate this tooling and have no changelog entry, so the first `publish` run intentionally skips tagging them rather than creating a release with empty notes; tagging starts from the next real version bump.

## License

You may use, modify, and distribute monkeybot under the MIT license. See the [`LICENSE`](LICENSE) file for details.
