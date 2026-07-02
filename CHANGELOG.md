# Changelog

All notable changes to monkey-bot (`monkeybot`) are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **Context curation** — Memory-only: the curator no longer selects skills. Curation triggers on `memory_threshold` only. Removed `skill_threshold`, `max_skills`, and related env vars. Skills are no longer injected into the system prompt; the harness directs agents to call `list_skills` before skill-backed work.
- **Pre-flight prompt tokens** — Summarization threshold and `estimated_prompt_tokens` (usage DB, SSE, `GET /usage`) use each provider's tokenizer / count API (`Provider.count_input_tokens`): Vertex Gemini `countTokens`, Anthropic `messages.count_tokens`, OpenAI `tiktoken` on the Chat Completions payload. OpenAI installs should include the `openai` extra (adds `tiktoken`).
- **Configurable history summarization model** — `CONTEXT_SUMMARIZATION_MODEL` and optional
  `model.summarization_model` in `monkeybot.yaml` (via runtime env) or `TurnContext.summarization_model`
  select the model id for sync context compression; main turn still uses `ctx.model`.

### Added
- **Docker baseline (Step 3)** — Root [`docker-compose.yml`](docker-compose.yml), default OpenSandbox config [`docker/opensandbox.docker.toml`](docker/opensandbox.docker.toml), [`.env.example`](.env.example); [`docker/Dockerfile`](docker/Dockerfile) adds `HEALTHCHECK`, drops `PYTHONPATH` to `src/`, ensures `/app/data` and `/app/skills`. Optional private deploy helpers live under `internal/` (gitignored; not shipped in the public OSS tree). See [`docs/cloud-deployment-design.md`](docs/cloud-deployment-design.md) Step 3.
- **FastAPI SSE gateway** (`monkeybot.gateway.sse`) — `POST /sessions`,
  `GET /sessions/{id}/events` (SSE stream with `Last-Event-ID` resume),
  `POST /sessions/{id}/reply`, `GET /health`. SQLite-backed session bus.
- **Owned agent loop** (`monkeybot.core.runtime.loop`) with streaming provider integration,
  tool execution, history append, and inspector hooks. Per-turn usage recorded.
- **Harness fixed prompt** (`monkeybot.core.prompts.harness_prompt`) — non-overridable
  system block describing tool names, paths, MCP naming, and skill usage,
  appended after the bot's `AGENT.md`.
- **Memory** (`monkeybot.core.memory`) — markdown files under `MEMORY_PATH`
  with optional `INDEX.md` snapshotted into context; `search_memory` for
  on-demand lookup; mid-turn refresh via `refresh_memory_index()` in
  `monkeybot.core.context`.
- **Memory organizer** (`monkeybot.core.memory.organizer`) — async
  post-turn classifier that updates `INDEX.md` and routes new entries to the
  right markdown file.
- **Context curator** (`monkeybot.core.context.curator`) — optional secondary
  LLM pass that narrows memory lines injected into the system prompt; main-loop
  only, never runs in subagents. Configured via `CONTEXT_CURATION_*` env vars.
- **Context-window safety** — pre-call token counting against
  `MODEL_CONTEXT_WINDOW`, sync history summarization at threshold, and tool-result
  spill to `.monkeybot/spill/{thread_id}/{call_id}.txt` with capped in-history
  text + path hint. Spill dir cleaned at next `run()` start.
- **Subagents** (`subagent_proto.py`, `subagent_worker.py`) — parallel `task`
  tool that fans out work over `asyncio.gather`; results appended in stable
  `call_id` order.
- **MCP** (`mcp_client.py`, `ports_mcp.py`) — stdio + streamable HTTP MCP
  servers loaded from `MCP_CONFIG`; tools exposed as `server__tool`.
- **Skills** (`monkeybot.core.context._discover_skills`,
  `monkeybot.skills.{loader,executor}`) — folder-based skills under
  `SKILLS_PATH` with `SKILL.md` per skill.
- **Workspace tools** (`workspace_tools.py`, `workspace_service.py`) —
  read/write/edit/find under a configurable workspace scope; paths under
  `.monkeybot` bypass `WORKSPACE_WRITE_SCOPE_REL` so spill and harness files
  remain writable.
- **Provider adapters** — Vertex / Gemini (`providers/gemini.py`),
  OpenAI (`providers/openai.py`), Anthropic (`providers/claude.py`),
  Anthropic-on-Vertex (`providers/vertex_claude.py`). Resolved via
  `monkeybot.core.config.get_provider_config()` returning a `ProviderConfig`.
- **Demo agent** under `demo_agent/` — self-contained sample agent project
  (own `pyproject.toml` + `.venv`, harness via editable path dependency) for
  trying out providers, OpenSandbox, observability, Firestore, and skills.
  Talk to it with `monkeybot run` / `monkeybot chat` from the CLI.
- **Reference bot** at `bots/example-bot/` (`AGENT.md`, `MEMORY.md`,
  `config.yaml`).
- **Default skills** under `.agents/skills/` (`file-ops`, `memory-search`,
  `search-web`, `self-improve`).
- **Cloud Run deploy helper** — previously `deploy.sh` + Cloud Build + `docker/Dockerfile`; optional copies under gitignored `internal/`; public baseline is `docker-compose.yml` + design doc Step 3.
- **Docs (v2)**: `docs/getting-started.md` and `docs/skills.md`. Configuration
  reference lives in root `.env.example`.
- **Test + bench infra**: `tests/` (pytest + pytest-asyncio) and
  `testing/bench.py` for end-to-end harness benchmarks.

### Changed
- **Packaging:** `pyproject.toml` authors set to `human+machine`; repository
  URLs point at `https://github.com/human-and-machine/monkey-bot`. Build
  backend is `hatchling`; package layout is `src/monkeybot`.
- **Default `SKILLS_PATH`** is `./.agents/skills` (matches the shipped
  `.env.example` and `docker/Dockerfile`).
