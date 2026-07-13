# monkeybot

<div align="center">
  <img src="docs/assets/logo.png" alt="monkeybot Logo" width="600" />

  <br />

  *Multi-cloud agent harness — GCP-first docs, portable runtime*

  [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

monkeybot is an owned agent runtime for tool-using LLMs: a FastAPI SSE gateway, pluggable storage, MCP tools, skills, and memory. Run locally with zero cloud dependencies, or ship the same container image anywhere Docker runs.

Docs and examples are **GCP-first** (Vertex, GCS, Cloud Run); AWS paths ship in the Pattern guides. See [Cloud deployment — Positioning](docs/cloud-deployment-design.md#positioning).

> [!NOTE]
> Skills under `SKILLS_PATH` execute Python code — only add skills from trusted sources.

## Quick start

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv tool install monkeybot-cli

monkeybot new --dest ./my-agent --provider openai --yes
cd my-agent
uv sync
cp .env.example .env   # add provider keys
monkeybot doctor
monkeybot chat
```

Upgrade the CLI with `uv tool upgrade monkeybot-cli`. Full walkthrough: **[Getting Started](docs/getting-started.md)**.

## What you get

- **SSE gateway** — sessions, streaming, health; wire your own UI or use the CLI
- **Realtime talk** — WebSocket voice/text via `monkeybot talk`
- **MCP + skills** — stdio/HTTP tools and trusted Python skill bundles
- **Pluggable storage** — SQLite by default; Postgres, Firestore, GCS, S3
- **Multi-provider** — Gemini/Vertex, OpenAI, Anthropic, Bedrock, Ollama, and more

## CLI

| Command | Purpose |
|---|---|
| `monkeybot new` | Scaffold config, workspace, agent `pyproject.toml`, `.env.example` |
| `monkeybot validate` | Check `monkeybot.yaml`, paths, and MCP config |
| `monkeybot doctor` | Verify Python, extras, credentials, and port |
| `monkeybot run` | Start the SSE gateway in the foreground |
| `monkeybot chat` | Talk over SSE (spawns the gateway; `--attach` to join one) |
| `monkeybot talk` | Realtime voice/text over WebSocket (`--text` for typed input) |

```bash
monkeybot chat            # spawn gateway + REPL
monkeybot run             # terminal 1: gateway with logs
monkeybot chat --attach   # terminal 2: attach to it
```

Provider extras live on the **agent** `pyproject.toml` (`monkeybot[<provider>]`), not the global CLI. See [Getting Started](docs/getting-started.md) and [Features — CLI](docs/features.md#16-cli).

## Agent skill

```bash
npx skills add human-plus-machine/monkeybot --skill monkeybot
```

Walks through install, scaffold, config, and first chat. Source: [`cli/skills/monkeybot/`](cli/skills/monkeybot/).

## Documentation

| Guide | Description |
|---|---|
| [Getting Started](docs/getting-started.md) | Install, configure, sessions + SSE from the CLI |
| [Features](docs/features.md) | Architecture, capabilities, CLI, task queue |
| [SSE gateway and custom UI](docs/sse-gateway-ui.md) | HTTP + SSE endpoints, event types, CORS |
| [Skills](docs/skills.md) | Skill directory layout and `SKILL.md` discovery |
| [Model Context Protocol](docs/mcp.md) | MCP config, env interpolation, OAuth2 |
| [Cloud deployment](docs/cloud-deployment-design.md) | Positioning + Patterns A–D index |
| [Live evals](docs/live-evals.md) | Smoke suite, local + CI scorecard |
| [Observability](docs/observability-runbook.md) | OpenTelemetry tracing runbook |

Integrations matrix and roadmap: [CHANGELOG](CHANGELOG.md) · [BACKLOG](BACKLOG.md).

## Contributing

Clone only if you are changing the harness. Setup, checks, and the release workflow are in **[CONTRIBUTING.md](CONTRIBUTING.md)**. Live eval smoke fixture: [`evals/smoke_agent/`](evals/smoke_agent/).

## Support

Search or open an issue in the [GitHub issue tracker](https://github.com/human-plus-machine/monkeybot/issues).

## License

MIT — see [`LICENSE`](LICENSE).
