---
name: monkeybot-setup
description: Create, configure, validate, and locally chat with a MonkeyBot agent using the monkeybot CLI. Use when setting up a new MonkeyBot workspace, scaffolding monkeybot_config/, validating configuration, or smoke-testing the agent from the terminal.
---

# MonkeyBot Setup

Orchestrate the **monkeybot** CLI (standalone package in the repo `cli/`). Never hand-write `monkeybot.yaml` — always use CLI commands and parse `--json` output for self-correction.

## Prerequisites

- Python 3.11+, `uv`
- Install harness: `uv sync` in the monkeybot repo root
- Install CLI: `cd cli && uv sync`
- Run commands: `cd cli && uv run monkeybot <subcommand>`

## Workflow

### 1. Interview the user

Ask (or infer from context):

- Bot purpose / name
- Provider: `gemini` | `openai` | `anthropic` | `vertex-claude` | `aws_bedrock` | `huggingface`
- Model id
- Whether they need MCP servers or custom skills

### 2. Scaffold

```bash
uv run monkeybot new --dest /path/to/bot --provider gemini --model gemini-3-flash --yes
```

Creates `monkeybot_config/`, `workspace/` (file-tool sandbox), `workspace/skills` → `.agents/skills`, `data/memory/`, and `scripts/setup-workspace.sh`. Use `--force` only when overwriting is explicitly requested.

**Subagents (`task` tool):** share the parent `AGENT.md` (or `subagent.agent_md` in yaml). Relative paths resolve from the bot project root, not `workspace/`. Specialize subagents via `task` / `context`, not separate agent type folders.

**Parallel `task` fan-out:** SQLite can hit `database is locked` with concurrent subagents. For local parallel work, use Postgres (`uv sync --extra postgres` in the harness repo) and set `DB_URL=postgresql://...` in `.env`.

### 3. Author AGENT.md

Edit `monkeybot_config/AGENT.md` with the user's system prompt. Do **not** put secrets in YAML.

### 4. Validate (loop until clean)

```bash
uv run monkeybot validate --json --cwd /path/to/bot
```

- Exit `0` + `"ok": true` → proceed
- On failure: read `checks[]` by stable `id`, apply `remediation`, re-run
- Never patch YAML by hand when `monkeybot new` or documented fields can fix it

### 5. Doctor

```bash
uv run monkeybot doctor --json --cwd /path/to/bot
```

Ensure provider extra installed and credentials present (`.env` from `.env.example`).

### 6. Smoke test

Terminal A:

```bash
uv run monkeybot run --cwd /path/to/bot
```

Terminal B (or `--spawn`):

```bash
uv run monkeybot chat --url http://127.0.0.1:8080 --cwd /path/to/bot
```

One successful turn confirms the setup.

## Rules

- **CLI owns scaffolding** — do not duplicate template files manually
- **Secrets in `.env` only** — never commit API keys to `monkeybot.yaml`
- **Parse JSON** — key off `checks[].id`, not free-form messages
- **HTTP/SSE for chat** — the CLI talks to the gateway process; do not import harness gateway code

## Command reference

| Command | Purpose |
|---------|---------|
| `new` | Scaffold `monkeybot_config/`, `workspace/`, `data/memory/`, `.agents/skills/`, `.env.example` |
| `validate` | Config + paths + MCP shape (`--check-mcp` for network) |
| `doctor` | Python, provider extra, credentials, port |
| `run` | Start SSE gateway subprocess |
| `chat` | Interactive terminal client (Option A: SSE) |
