---
name: monkeybot
description: Entry point for MonkeyBot — install the CLI from GitHub if needed, then scaffold, configure, validate, and chat with an agent. Use when a user installs this skill via `npx skills add human-plus-machine/monkeybot --skill monkeybot`, sets up MonkeyBot for the first time, clones the repo, installs the monkeybot CLI, scaffolds monkeybot_config/, explains monkeybot.yaml options, or smoke-tests from the terminal.
---

# MonkeyBot

Guide a new user from **only this skill** to a working agent. Installing the skill (`npx skills add …`) gives you these instructions — it does **not** install the MonkeyBot repo or CLI. **Always run Tier 0 first** unless you already know the toolchain is ready.

## Tier 0 — Bootstrap toolchain

Run these checks before anything else. Do not skip to Tier 1 until the CLI probe succeeds.

### 0. Detect what's already installed

Run in order (stop at the first success):

```bash
# A) Global CLI on PATH (uv tool install)
command -v monkeybot && monkeybot --help

# B) Known repo checkout (user or prior session set MONKEYBOT_HOME)
test -n "$MONKEYBOT_HOME" && test -f "$MONKEYBOT_HOME/cli/pyproject.toml" \
  && cd "$MONKEYBOT_HOME/cli" && uv run monkeybot --help

# C) Common clone locations
for d in "$HOME/monkeybot" "$HOME/code/monkeybot" "$HOME/monkey-bot"; do
  test -f "$d/cli/pyproject.toml" && echo "found:$d" && break
done
```

**If a repo path is found**, export it for the rest of the session:

```bash
export MONKEYBOT_HOME=/path/to/monkey-bot   # directory that contains cli/ and pyproject.toml
```

**CLI invocation for this session** — use whichever works:

| Situation | Command prefix |
|---|---|
| `monkeybot` on PATH | `monkeybot <subcommand>` |
| Repo only (no global install) | `cd "$MONKEYBOT_HOME/cli" && uv run monkeybot <subcommand>` |

Prefer `uv run` from `cli/` when unsure — it always uses the editable harness next to the repo.

### 1. Install prerequisites (only if missing)

| Tool | Check | Install |
|---|---|---|
| **uv** | `command -v uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` then `export PATH="$HOME/.local/bin:$PATH"` |
| **Python 3.11+** | `uv python find 3.11` or `python3 --version` | `uv python install 3.12` |
| **git** | `command -v git` | OS package manager (`brew install git`, `apt install git`, etc.) |

Tell the user what you're installing and why. On macOS/Linux you can run these yourself; on Windows, point them at [uv](https://docs.astral.sh/uv/) and Git for Windows.

### 2. Clone and install MonkeyBot (only if Tier 0 detect failed)

MonkeyBot is **not on PyPI yet** — clone the GitHub repo and install the CLI from source.

```bash
# Pick a directory (default ~/monkeybot); ask if the user has a preference
export MONKEYBOT_HOME="${MONKEYBOT_HOME:-$HOME/monkeybot}"

git clone https://github.com/human-and-machine/monkey-bot.git "$MONKEYBOT_HOME"
cd "$MONKEYBOT_HOME"

# Harness (required — CLI depends on editable ../monkeybot)
uv sync

# CLI package
cd cli && uv sync
```

**Optional — put `monkeybot` on PATH** (recommended for repeat use):

```bash
cd "$MONKEYBOT_HOME/cli"
uv tool install --editable .
```

Re-run the detect probe. `monkeybot --help` or `uv run monkeybot --help` from `cli/` must succeed before Tier 1.

### 3. Skill-only users

If the user arrived via skills.sh:

```bash
npx skills add human-plus-machine/monkeybot --skill monkeybot
```

…they have **this skill only**. Walk them through Tier 0 explicitly: clone → `uv sync` (root) → `uv sync` (`cli/`) → verify CLI. Then continue to Tier 1.

**Persist `MONKEYBOT_HOME`** in the user's shell profile or document it in the bot project README so future sessions find the repo without re-cloning.

## How config works (tell the user this first)

- **CLI scaffolds, YAML customizes, then validate.** `monkeybot new` copies packaged defaults from the `monkeybot` package and sets `model.provider` / `model.name`. After that, customization happens by editing `monkeybot_config/monkeybot.yaml` and `.env`, then re-running `monkeybot validate --json`.
- **Three config surfaces:**
  - `monkeybot_config/monkeybot.yaml` — non-secret settings (model, paths, gateway, behavior).
  - `.env` — secrets and machine-local paths (API keys, GCP project, DB URL).
  - sidecars: `monkeybot_config/AGENT.md` (system prompt), `mcp.json` (MCP servers), `command_allowlist.yaml`.
- **Precedence — important:** environment variables and `.env` win over `monkeybot.yaml`. If a user edits YAML but nothing changes, suspect a stale `.env` shadowing it (the YAML→env mapping lives in `runtime_env.py:ENV_MAP`).
- **Defaults are fine on day one.** Most first-time users only touch `model`, `.env` credentials, and `AGENT.md`.

## Running CLI commands

After Tier 0, every command below uses your resolved prefix (`monkeybot …` or `cd "$MONKEYBOT_HOME/cli" && uv run monkeybot …`). Examples use `monkeybot` for brevity — substitute as needed.

## Tier 1 — Get it talking

The minimum path to one successful chat turn. Do this for every new user.

### 1. Quick interview

Ask (or infer):

- Bot purpose / name → goes in `AGENT.md`
- Provider + model (see provider table below)

### 2. Scaffold

```bash
monkeybot new --dest /path/to/bot --provider gemini --model gemini-3-flash --yes
```

Creates `monkeybot_config/`, `workspace/` (file-tool sandbox), `workspace/skills` → `skills/`, `data/memory/`, `.env.example`, and `scripts/setup-workspace.sh`. Use `--force` only when overwriting is explicitly requested.

### 3. Credentials + system prompt

- Copy `.env.example` → `.env` and fill in the keys for your provider (table below).
- Edit `monkeybot_config/AGENT.md` with the bot's system prompt. **Never put secrets in YAML.**

### Provider table

| YAML `model.provider` | `.env` credentials (any one) | Install extra (in the **agent project**) |
|---|---|---|
| `gemini` (or `vertex`) | `GEMINI_API_KEY`, or `GOOGLE_APPLICATION_CREDENTIALS`, or `GCP_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` (ADC) | `uv sync --extra gemini` |
| `openai` | `OPENAI_API_KEY` | `uv sync --extra openai` |
| `anthropic` | `ANTHROPIC_API_KEY` | `uv sync --extra claude` |
| `vertex-claude` | `GCP_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID` (ADC) | `uv sync --extra vertex-claude` |
| `aws_bedrock` | `AWS_ACCESS_KEY_ID` / `AWS_PROFILE` + `AWS_REGION` | `uv sync --extra bedrock` |
| `huggingface` | `HF_TOKEN` (or `HUGGINGFACE_API_KEY`) | `uv sync --extra huggingface` |

**Agent-first dependencies.** The CLI is thin — it does **not** install provider/storage extras globally. Declare them on the **agent project**: give the agent a `pyproject.toml` listing `monkeybot[bedrock,postgres,...]` and run `uv sync` there (this creates `.venv`). `monkeybot run` / `chat` spawn the gateway from that project's interpreter (`.venv/bin/python`, else `uv run python`), and `doctor` checks extras in that same interpreter. For a config-only tree (just `monkeybot_config/`, no `pyproject.toml`) the gateway falls back to the CLI's interpreter, so extras must be installed in the CLI env (`uv tool install --with 'monkeybot[<extra>]' monkeybot-cli`).

`doctor` is the source of truth for credentials and extras — when in doubt, run it and read the `remediation` field (it points at the agent project, not the CLI env).

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

Terminal A:

```bash
monkeybot run --cwd /path/to/bot
```

Terminal B (or `--spawn`):

```bash
monkeybot chat --url http://127.0.0.1:8080 --cwd /path/to/bot
```

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
| "Use specialist agents" | `subagents[]` + `monkeybot_config/agents/*.md` |
| "Connect external tools" | `mcp.json` (`mcpServers` object), then `validate --check-mcp` |
| "Control cost / context size" | `model.*`, `context_curation.*` |
| "Restrict dangerous commands" | `tools.denied_patterns`, `command_allowlist.yaml` |
| "Multiple environments" | top-level `includes:` fragments |

**Subagents (`task` tool):** share the parent `AGENT.md` (or `subagent.agent_md`). Relative paths resolve from the bot project root, not `workspace/`. Specialize via `task` / `context`, not separate agent type folders. For parallel `task` fan-out, prefer Postgres: `uv sync --extra postgres` in the **agent project**, then `DB_URL=postgresql://...` in `.env`.

**Observability** is mostly env + `uv sync --extra observability` + an OTel collector — not `monkeybot.yaml`. See `docs/observability-runbook.md`.

## Rules

- **Tier 0 before Tier 1** — verify or install the CLI and harness before scaffolding a bot.
- **CLI owns scaffolding** — do not duplicate template files manually.
- **Secrets in `.env` only** — never commit API keys to `monkeybot.yaml`.
- **Validate after every edit** — re-run `validate --json` whenever you change config.
- **Parse JSON** — key off `checks[].id`, not free-form messages.
- **HTTP/SSE for chat** — the CLI talks to the gateway process; do not import harness gateway code.

## Command reference

| Command | Purpose |
|---------|---------|
| `new` | Scaffold `monkeybot_config/`, `workspace/`, `data/memory/`, `skills/`, `.env.example` |
| `validate` | Config + paths + MCP shape (`--check-mcp` for network) |
| `doctor` | Python, provider extra, credentials, port |
| `run` | Start SSE gateway subprocess |
| `chat` | Interactive terminal client (SSE) |
