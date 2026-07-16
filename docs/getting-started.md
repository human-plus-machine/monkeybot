# Getting Started (monkeybot v2)

Create an agent anywhere on your machine with the global CLI — no monkeybot repo clone required. The agent owns its `pyproject.toml` / `.venv`; the CLI stays thin.

Session routes are **not** authenticated; do not expose the gateway to the public internet without putting auth or a private network in front of it.

**Cloud note:** Walkthroughs often mention Gemini/Vertex because our **GCP paths are the most documented**, but the same gateway runs on OpenAI, Anthropic, Bedrock, Ollama, and other shipped providers — set `model.provider` in `monkeybot.yaml`, add the matching `monkeybot[<extra>]` dependency to the agent `pyproject.toml` and run `uv sync`, then put credentials in `.env`. See [Cloud deployment — Positioning](cloud-deployment-design.md#positioning).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| [uv](https://docs.astral.sh/uv/) | Installs the CLI and agent deps |
| Python | 3.11+ (uv can install it: `uv python install 3.12`) |

---

## 1. Install the CLI

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if needed
uv tool install monkeybot-cli
monkeybot --help
```

Upgrade later with `uv tool upgrade monkeybot-cli`.

---

## 2. Scaffold an agent

```bash
mkdir -p ~/agents && cd ~/agents
monkeybot new --dest ./my-agent --provider openai --yes
# Or omit --yes / --provider to pick provider + optional extras interactively
cd my-agent
```

This creates an agent-root layout: `monkeybot_config/` and `skills/` are
committed inputs; `workspace/` is agent-writable; `data/memory/` is local
runtime state; and the project includes a `Dockerfile`, `.dockerignore`, and
non-packaged agent `pyproject.toml`. The browser MCP package and browser skill
are bundled but disabled by default. See [Agent project layout](agent-layout.md)
for the complete zone, deployment, and migration contract.

---

## 3. Sync deps and credentials

```bash
uv sync
cp .env.example .env
# Edit .env with API keys / ADC for your provider
# Edit monkeybot_config/AGENT.md with the bot system prompt
```

Important knobs (see **`monkeybot_config/monkeybot.example.yaml`** for all sections and comments):

| YAML section | Purpose |
|---|---|
| `paths.workspace_root` | Agent-writable file-tool sandbox (default `./workspace`). |
| `paths.agent_md` | System prompt file (default `./monkeybot_config/AGENT.md`). |
| `paths.memory_storage_uri` | Durable markdown memory root (`local://…`, `gcs://…`, `s3://…`); optional `INDEX.md` is surfaced in the prompt. Legacy `paths.memory_path` still maps to `MEMORY_PATH`. |
| `paths.skills_path` | Read-only trusted skill bundle root (default `./skills`). |
| `paths.db_url` | SQLite URL for conversation + usage. |
| `paths.mcp_config` / `paths.command_allowlist_config` | MCP map and run_command allowlist policy path. |
| `subagents.personas[].agent_md` | Optional persona prompt for `task(subagent_type=...)`; without a type, inherits `paths.agent_md`. |
| `model.provider` / `model.name` | Provider and model id (`gemini`, `openai`, `anthropic`, …). |
| `runtime.port` | Gateway listen port. |

All relative YAML paths resolve from the agent root — the directory containing
`monkeybot_config/` — not from the command's current directory. Startup finds
the root from `MONKEYBOT_CONFIG` or the nearest parent containing
`monkeybot_config/`, loads that root's `.env`, then applies YAML values only to
environment variables that are still unset. `MONKEYBOT_CONFIG` and the other
environment overrides are absolute-path escape hatches for containers; see
[Agent project layout](agent-layout.md#path-resolution).

---

## 4. Validate, doctor, chat

```bash
monkeybot validate
monkeybot doctor
monkeybot chat            # spawns the gateway, connects, cleans up on exit
```

`monkeybot doctor` also prints the resolved agent root, zone paths, storage
backends, browser state, and sandbox mode. If it finds a legacy `skills`
directory nested in `workspace`, use its migration preview and resolve any
reported collisions before moving files.

To watch gateway logs in another terminal:

```bash
monkeybot run             # terminal 1
monkeybot chat --attach   # terminal 2
```

---

## 5. Call the HTTP API (optional)

Session and SSE routes do **not** require an `Authorization` header. Default scaffold port is `8080`.

### Create a session

```bash
curl -sS -X POST "http://127.0.0.1:8080/sessions" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Optional body fields: `session_id` (client-chosen id), `agent_md` (path override for this session’s prompt file).

### Open the SSE stream

In another terminal, stream events (reconnect with `Last-Event-ID` as needed):

```bash
curl -sN "http://127.0.0.1:8080/sessions/<SESSION_ID>/events"
```

### Send a message

```bash
curl -sS -X POST "http://127.0.0.1:8080/sessions/<SESSION_ID>/reply" \
  -H "Content-Type: application/json" \
  -d '{"request_id":"req-1","message":"Hello"}'
```

Agent output arrives as **SSE `data:`** JSON events on the stream.

### Health

```bash
curl -sS "http://127.0.0.1:8080/health"
```

---

## Developing the harness (contributors)

Clone the repo only when changing monkeybot itself:

```bash
git clone https://github.com/human-plus-machine/monkeybot.git
cd monkeybot && uv sync
cd cli && uv sync
uv tool install --editable .
```

For live eval smoke against this checkout, see [`evals/smoke_agent/`](../evals/smoke_agent/).

---

## Next steps

- [Skills](skills.md) — trusted skills, read-only file-tool access, and `SKILL.md` layout.
- [Browser MCP](browser-mcp.md) — bundled-but-disabled browser controls and CDP modes.
- [Agent project layout](agent-layout.md) — zones, Docker, remote sandbox contract, and deployment matrix.
- [Model Context Protocol](mcp.md) — configuration, environment variable interpolation, OAuth2 flows, and startup diagnostics.
