---
name: monkeybot
description: Entry point for monkeybot — install the CLI from PyPI if needed, then scaffold, configure, validate, and chat with an agent. Use when a user installs this skill via `npx skills add human-plus-machine/monkeybot --skill monkeybot`, sets up monkeybot for the first time, installs the monkeybot CLI, scaffolds monkeybot_config/, explains monkeybot.yaml options, or smoke-tests from the terminal.
---

# monkeybot

Guide a new user from **only this skill** to a working agent. Installing the skill (`npx skills add …`) gives you these instructions — it does **not** install the monkeybot CLI. **Always run Tier 0 first** unless you already know the toolchain is ready. Day-1 users do **not** need to clone the monkeybot repo.

## Tier 0 — Bootstrap toolchain

Run these checks before anything else. Do not skip to Tier 1 until the CLI probe succeeds.

### 0. Detect what's already installed

```bash
command -v monkeybot && monkeybot --help
```

If that works, skip to Tier 1.

### 1. Install prerequisites (only if missing)

| Tool | Check | Install |
|---|---|---|
| **uv** | `command -v uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` then `export PATH="$HOME/.local/bin:$PATH"` |
| **Python 3.11+** | `uv python find 3.11` or `python3 --version` | `uv python install 3.12` |

Tell the user what you're installing and why. On macOS/Linux you can run these yourself; on Windows, point them at [uv](https://docs.astral.sh/uv/).

### 2. Install the global CLI

```bash
uv tool install monkeybot-cli
monkeybot --help
```

Upgrade later with `uv tool upgrade monkeybot-cli`.

**Do not** tell day-1 users to clone `human-plus-machine/monkeybot` unless they are contributing to the harness itself (see Contributing below).

### 3. Skill-only users

If the user arrived via skills.sh:

```bash
npx skills add human-plus-machine/monkeybot --skill monkeybot
```

…they have **this skill only**. Walk them through Tier 0: install `uv` → `uv tool install monkeybot-cli` → verify `monkeybot --help`. Then continue to Tier 1.

## How config works (tell the user this first)

- **CLI scaffolds, YAML customizes, then validate.** `monkeybot new` copies packaged defaults from the `monkeybot-cli` package (`monkeybot_cli.scaffold_defaults`) and sets `model.provider` / `model.name`. After that, customization happens by editing `monkeybot_config/monkeybot.yaml` and `.env`, then re-running `monkeybot validate --json`.
- **Three config surfaces:**
  - `monkeybot_config/monkeybot.yaml` — non-secret settings (model, paths, gateway, behavior).
  - `.env` — secrets and machine-local paths (API keys, GCP project, DB URL).
  - sidecars: `monkeybot_config/AGENT.md` (system prompt), `mcp.json` (MCP servers), `command_allowlist.yaml`.
- **Precedence — important:** environment variables and `.env` win over `monkeybot.yaml`. If a user edits YAML but nothing changes, suspect a stale `.env` shadowing it (the YAML→env mapping lives in `runtime_env.py:ENV_MAP`).
- **Defaults are fine on day one.** Most first-time users only touch `model`, `.env` credentials, and `AGENT.md`.

## Running CLI commands

After Tier 0, every command below uses `monkeybot` on PATH.

## Tier 1 — Get it talking

The minimum path to one successful chat turn. Do this for every new user.

### 1. Quick interview

Ask (or infer):

- Bot purpose / name → goes in `AGENT.md`
- Provider + model (see provider table below)
- Optional features (postgres, sandbox, observability, …) → `--with` or interactive menus

### 2. Scaffold

```bash
monkeybot new --dest /path/to/bot --provider openai --yes
# Interactive (menus for provider + extras):
# monkeybot new --dest /path/to/bot
# Non-interactive extras:
# monkeybot new --dest /path/to/bot --provider openai --with postgres,sandbox --yes
```

Creates `monkeybot_config/`, read-only `skills/`, writable `workspace/`,
`memory/`, `.env.example`, a Dockerfile, and an agent `pyproject.toml`.
Use `--force` only when overwriting is explicitly requested.

Then:

```bash
cd /path/to/bot
uv sync
```

### 3. Credentials + system prompt

- Copy `.env.example` → `.env` and fill in the keys for your provider (table below).
- Edit `monkeybot_config/AGENT.md` with the bot's system prompt. **Never put secrets in YAML.**

### Provider table

| YAML `model.provider` | `.env` credentials (any one) | Add to agent `pyproject.toml` deps, then `uv sync` |
|---|---|---|
| `gemini` (or `vertex`) | `GEMINI_API_KEY`, or `GOOGLE_APPLICATION_CREDENTIALS`, or `GCP_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` (ADC) | `monkeybot[gemini]` |
| `openai` | `OPENAI_API_KEY` | `monkeybot[openai]` |
| `anthropic` | `ANTHROPIC_API_KEY` | `monkeybot[claude]` |
| `vertex-claude` | `GCP_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID` (ADC) | `monkeybot[vertex-claude]` |
| `aws_bedrock` | `AWS_ACCESS_KEY_ID` / `AWS_PROFILE` + `AWS_REGION` | `monkeybot[bedrock]` |
| `huggingface` | `HF_TOKEN` (or `HUGGINGFACE_API_KEY`) | `monkeybot[huggingface]` |
| `ollama` | None required — `OLLAMA_BASE_URL` (default `http://localhost:11434`) for a non-default server | `monkeybot[ollama]` |

**Agent-first dependencies.** The CLI is thin — it does **not** install provider/storage extras globally. `monkeybot new` scaffolds a `pyproject.toml` with the selected provider (and any `--with` extras). Run plain `uv sync` in the agent directory. `monkeybot run` / `chat` spawn the gateway from that project's interpreter (`.venv/bin/python`, else `uv run python`), and `doctor` checks extras in that same interpreter. For a config-only tree (just `monkeybot_config/`, no `pyproject.toml`) the gateway uses the CLI's interpreter when it already has MonkeyBot 3.x. If memory is enabled and that interpreter cannot import MemPalace, `run` / `chat` provision a cached CLI-managed venv (pinned to the running core; never rewrites a `pyproject.toml`). Otherwise extras must be installed in the CLI env (`uv tool install --with 'monkeybot[<extra>]' monkeybot-cli`). If memory is enabled and the CLI env has no MemPalace, `monkeybot run` provisions a cached runtime under `~/.cache/monkeybot/runtimes/` (pinned to the running MonkeyBot version) instead of rewriting any project files.

`doctor` is the source of truth for credentials and extras — when in doubt, run it and read the `remediation` field (add `monkeybot[<extra>]` to agent deps + `uv sync`).

### 4. Validate (loop until clean)

```bash
monkeybot validate --json --cwd /path/to/bot
```

- Exit `0` + `"ok": true` → proceed.
- On failure: read `checks[]` by stable `id`, apply `remediation`, re-run.

### 5. Doctor

```bash
monkeybot doctor --json --cwd /path/to/bot
```

Confirms Python version, provider extra installed, credentials present, and port free.

### 6. Smoke test

```bash
monkeybot chat --cwd /path/to/bot
```

Or split terminals: `monkeybot run --cwd /path/to/bot` then `monkeybot chat --attach --cwd /path/to/bot`.

One successful turn confirms Tier 1.

## Tier 2 — Customize (on demand)

Only reach for these when the interview surfaces a need. Each maps to a `monkeybot.yaml` section. **For purpose, defaults, examples, and the `validate`/`doctor` check id of every option, read [`references/config-sections.md`](references/config-sections.md).** Don't inline that reference here.

Decision → config map:

| User says… | Change |
|---|---|
| "Run many subagents in parallel" | `paths.db_url` → Postgres (SQLite locks under concurrency) |
| "I have a custom web UI" | `gateway.cors_allow_origins` |
| "Search the web" | `web_search.backend` + `.env` keys (Tavily/Firecrawl) |
| "Run untrusted code" | `sandbox.enabled` + `SANDBOX_API_KEY` |
| "Use specialist agents" | `subagents.personas` + `monkeybot_config/agents/*.md` |
| "Connect external tools" | `mcp.json` (`mcpServers` object), then `validate --check-mcp` |
| "Control cost / context size" | `model.*`, `context_curation.*` |
| "Restrict dangerous commands" | `tools.denied_patterns`, `command_allowlist.yaml` |
| "Multiple environments" | top-level `includes:` fragments |

**Subagents (`task` tool):** without `subagent_type`, share the parent `AGENT.md`. With a persona, use that persona's `agent_md`. Relative paths resolve from the bot project root, not `workspace/`. Specialize via `task` / `context` or named personas. For parallel `task` fan-out, prefer Postgres: add `monkeybot[postgres]` to the **agent** `pyproject.toml` dependencies, run `uv sync`, then set `DB_URL=postgresql://...` in `.env`.

**Observability** is mostly env + add `monkeybot[observability]` to agent deps + `uv sync` + an OTel collector — not `monkeybot.yaml`. See `docs/observability-runbook.md`.

## Contributing / developing the harness

Only if the user is changing monkeybot itself (not creating an agent):

```bash
git clone https://github.com/human-plus-machine/monkeybot.git
cd monkeybot && uv sync
cd cli && uv sync
uv tool install --editable .
```

## Rules

- **Tier 0 before Tier 1** — verify or install the global CLI before scaffolding a bot.
- **CLI owns scaffolding** — do not duplicate template files manually.
- **Secrets in `.env` only** — never commit API keys to `monkeybot.yaml`.
- **Validate after every edit** — re-run `validate --json` whenever you change config.
- **Parse JSON** — key off `checks[].id`, not free-form messages.
- **HTTP/SSE for chat** — the CLI talks to the gateway process; do not import harness gateway code.
- **No clone for day-1 users** — clone is for harness contributors only.

## Command reference

| Command | Purpose |
|---------|---------|
| `new` | Scaffold `monkeybot_config/`, `workspace/`, `memory/`, `skills/`, `pyproject.toml`, `.env.example` |
| `refresh` | Additive update of packaged YAML defaults on an existing agent (keeps AGENT.md, mcp.json, model) |
| `validate` | Config + paths + MCP shape (`--check-mcp` for network) |
| `doctor` | Python, provider extra, credentials, port |
| `run` | Start SSE gateway subprocess |
| `chat` | Interactive terminal client (SSE) |
| `talk` | Realtime WebSocket client (audio/text) |
