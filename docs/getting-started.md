# Getting Started (MonkeyBot v2)

Run the **SSE gateway** locally: FastAPI, SQLite conversation history, optional MCP tools, and a single system prompt from **AGENT.md**. Session routes are **not** authenticated; do not expose the gateway to the public internet without putting auth or a private network in front of it.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python | 3.11+ (3.12 recommended) |
| [uv](https://docs.astral.sh/uv/) | Used in this repo for installs and `uv run` |

For **Gemini / Vertex**, you need Google Cloud credentials and a project. Put values in a **`.env`** file in the repo root (optional) or export them in the shell — see the header of **`monkeybot_config/monkeybot.example.yaml`** for common variable names.

---

## 1. Install

From the repo root:

```bash
uv sync
```

---

## 2. Configure

**Option A — scaffold defaults** (from the repo root; skips files that already exist; add `--force` to overwrite):

```bash
uv run monkeybot-init-config
```

**Option B — manual copy** of the harness template (paths, model, gateway, curation, web search, sandbox — everything non-secret):

```bash
cp monkeybot_config/monkeybot.example.yaml monkeybot_config/monkeybot.yaml
```

If you used **Option A**, `monkeybot.yaml` is already created from the same template. Edit `monkeybot_config/monkeybot.yaml` for your layout. Create a **`.env`** in the repo root only when you need API keys, `GOOGLE_APPLICATION_CREDENTIALS`, or other secrets / machine-local overrides (variable names are listed in the header of `monkeybot.example.yaml`).

The gateway loads `.env` first, then applies `monkeybot_config/monkeybot.yaml` for any environment variable that is **still unset**. Use `MONKEYBOT_CONFIG` to point at a different YAML file. Optional **`includes:`** in that file lists extra YAML fragments (paths relative to the primary file’s directory) merged in order.

Important knobs (see **`monkeybot_config/monkeybot.example.yaml`** for all sections and comments):

| YAML section | Purpose |
|---|---|
| `paths.agent_md` | System prompt file (default `./monkeybot_config/AGENT.md`). |
| `paths.memory_storage_uri` | Durable markdown memory root (`local://…`, `gcs://…`, `s3://…`); optional `INDEX.md` is surfaced in the prompt. Legacy `paths.memory_path` still maps to `MEMORY_PATH`. |
| `paths.skills_path` | Skill bundle root. |
| `paths.db_url` | SQLite URL for conversation + usage. |
| `paths.mcp_config` / `paths.command_allowlist_config` | MCP map and run_command allowlist policy path. |
| `model.provider` / `model.name` | Provider and model id (`gemini`, `openai`, `fake`, …). |
| `runtime.port` | Gateway listen port. |

---

## 3. Author AGENT.md

Point `paths.agent_md` in `monkeybot_config/monkeybot.yaml` at a non-empty Markdown file (the repo ships `monkeybot_config/AGENT.md`). Its contents become the base system message for each turn, plus the optional memory index and skill list. See [Skills](skills.md) for adding capabilities under `paths.skills_path`.

---

## 4. Run the gateway

```bash
uv run python -m monkeybot.gateway.main
```

The gateway reads `PORT` (falls back to `GATEWAY_PORT`, then `8000`). The shipped `monkeybot_config/monkeybot.yaml` sets `runtime.port` to `8080`, so examples below use `8080`.

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

- [Skills](skills.md) — layout under `SKILLS_PATH` and `SKILL.md` per skill folder.
