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

This creates `monkeybot_config/`, `workspace/`, `data/memory/`, `.env.example`, and an agent `pyproject.toml` that depends on `monkeybot[<provider>]` (plus any `--with` extras).

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
| `paths.workspace_root` | File-tool sandbox (default `./workspace` when scaffolded via CLI). |
| `paths.agent_md` | System prompt file (default `./monkeybot_config/AGENT.md`). |
| `paths.memory_storage_uri` | Durable markdown memory root (`local://…`, `gcs://…`, `s3://…`); optional `INDEX.md` is surfaced in the prompt. Legacy `paths.memory_path` still maps to `MEMORY_PATH`. |
| `paths.skills_path` | Skill bundle root. |
| `paths.db_url` | SQLite URL for conversation + usage. |
| `paths.mcp_config` / `paths.command_allowlist_config` | MCP map and run_command allowlist policy path. |
| `subagent.agent_md` | Subagent prompt file (defaults to same as `paths.agent_md`). Relative paths use project root, not `workspace/`. |
| `model.provider` / `model.name` | Provider and model id (`gemini`, `openai`, `anthropic`, …). |
| `runtime.port` | Gateway listen port. |

The gateway loads `.env` first, then applies `monkeybot_config/monkeybot.yaml` for any environment variable that is **still unset**. Use `MONKEYBOT_CONFIG` to point at a different YAML file.

---

## 4. Validate, doctor, chat

```bash
monkeybot validate
monkeybot doctor
monkeybot chat            # spawns the gateway, connects, cleans up on exit
```

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

See also [`demo_agent/`](../demo_agent/) for an in-repo example agent.

---

## Next steps

- [Skills](skills.md) — layout under `SKILLS_PATH` and `SKILL.md` per skill folder.
- [Model Context Protocol](mcp.md) — configuration, environment variable interpolation, OAuth2 flows, and startup diagnostics.
