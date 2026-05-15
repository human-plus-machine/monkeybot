# Getting Started (MonkeyBot v2)

Run the **SSE gateway** locally: FastAPI, SQLite conversation history, optional MCP tools, and a single system prompt from **AGENT.md**. Session routes are **not** authenticated; do not expose the gateway to the public internet without putting auth or a private network in front of it.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python | 3.11+ (3.12 recommended) |
| [uv](https://docs.astral.sh/uv/) | Used in this repo for installs and `uv run` |

For **Gemini / Vertex**, you also need Google Cloud credentials and a project (see `.env.example`).

---

## 1. Install

From the repo root:

```bash
uv sync
```

---

## 2. Configure

Copy the example env file and set paths for the agent (and model credentials when using Gemini):

```bash
cp .env.example .env
```

Important variables (see `.env.example` for the full list — values below are what the shipped `.env.example` uses):

| Variable | Purpose |
|---|---|
| `AGENT_MD` | Path to your system prompt file (`./bots/example-bot/AGENT.md`). |
| `MEMORY_PATH` | Directory for long-term markdown memory (`./data/memory`). Optional `INDEX.md` there is surfaced as a memory index in the system prompt. |
| `SKILLS_PATH` | Root directory for skill bundles (`./.agents/skills`). |
| `DB_URL` | SQLite URL for conversation + usage tables (`sqlite:///data/monkeybot.db`). |
| `MCP_CONFIG` | Optional MCP server map (`./config/mcp.json`). |
| `COMMAND_TIERS_CONFIG` | Optional command-tier policy file (`./config/command_tiers.yaml`). |
| `MODEL_PROVIDER` | `google_vertexai` (default), `openai`, `anthropic`, or `vertex_anthropic`. |
| `MODEL_NAME` | Model id (e.g. `gemini-2.5-flash`). |
| `PORT` | Gateway port (`.env.example` uses `8080`; gateway falls back to `8000` if unset). |

---

## 3. Author AGENT.md

Point `AGENT_MD` at a non-empty Markdown file (the repo ships `bots/example-bot/AGENT.md`). Its contents become the base system message for each turn, plus the optional memory index and skill list. See [Skills](skills.md) for adding capabilities under `SKILLS_PATH`.

---

## 4. Run the gateway

```bash
uv run python -m monkeybot.gateway.main
```

The gateway reads `PORT` (falls back to `GATEWAY_PORT`, then `8000`). The shipped `.env.example` sets `PORT=8080`, so examples below use `8080`.

---

## 5. Call the HTTP API

Session and SSE routes do **not** require an `Authorization` header.

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

## Next steps

- [Skills](skills.md) — layout under `SKILLS_PATH`, `SKILL.md`, and `run.py` / `main.py`.
- Repository **`.env.example`** — optional MCP, command tiers, and Cloud Run-related variables.
