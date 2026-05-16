# Changelog

All notable changes to monkey-bot (`monkeybot`) are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
  LLM pass that narrows skills + memory snippets injected into the system
  prompt; main-loop only, never runs in subagents. Configured via
  `CONTEXT_CURATION_*` env vars.
- **Context-window safety** — pre-call token estimation against
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
- **Playground** under `playground/` — local gateway runner
  (`playground/agent/`) and Vite + React chat UI (`playground/chat-ui/`).
- **Reference bot** at `bots/example-bot/` (`AGENT.md`, `MEMORY.md`,
  `config.yaml`).
- **Default skills** under `.agents/skills/` (`file-ops`, `memory-search`,
  `search-web`, `self-improve`).
- **Cloud Run deploy helper** (`deploy.sh`) using Cloud Build +
  `docker/Dockerfile`.
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
