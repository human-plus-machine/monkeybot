# Changelog

All notable changes to monkeybot are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Repo-root `monkeybot_config_example/` — full-option human-readable config templates (`.example` filenames).
- `monkeybot new` also scaffolds `permissions.yaml`.

### Changed

- Scaffold templates and `run_new` moved into **monkeybot-cli** (`monkeybot_cli.scaffold` / `scaffold_defaults`). Harness `monkeybot.scaffold` is a compatibility shim; Docker installs the CLI to scaffold at image build.
- **monkeybot-cli** version bumped to **0.2.0**.

- README integrations tables aligned with v2.0.0 shipped scope (per CHANGELOG); removed speculative "Coming Soon" items to BACKLOG.md with a short near-term roadmap.
- Reframed public positioning: multi-cloud-capable harness with **GCP-first** docs (README, cloud deployment design, getting started, features, Pattern A guide). Clarifies this is not a universal integration catalog; documents AWS shipped paths alongside GCP examples.

## [2.0.0] - 2026-07-05

Major v2 rewrite: FastAPI SSE gateway, owned agent loop, pluggable storage, multi-provider support, CLI, evals, and cloud-ready deployment artifacts.

### Added

#### Gateway & runtime
- **2026-05-14** — FastAPI SSE gateway (`monkeybot.gateway.sse`): `POST /sessions`, `GET /sessions/{id}/events` (SSE with `Last-Event-ID` resume), `POST /sessions/{id}/reply`, `GET /health`. SQLite-backed session bus.
- **2026-05-14** — Owned agent loop (`monkeybot.core.runtime.loop`) with streaming provider integration, tool execution, history append, and inspector hooks. Per-turn usage recorded.
- **2026-05-14** — Harness fixed prompt (`monkeybot.core.prompts.harness_prompt`) — non-overridable system block describing tool names, paths, MCP naming, and skill usage, appended after the bot's `AGENT.md`.
- **2026-05-14** — Context-window safety: pre-call token counting against `MODEL_CONTEXT_WINDOW`, sync history summarization at threshold, and tool-result spill to `.monkeybot/spill/{thread_id}/{call_id}.txt` with capped in-history text + path hint.
- **2026-05-14** — Subagents (`subagent_proto.py`, `subagent_worker.py`) — parallel `task` tool that fans out work over `asyncio.gather`; results appended in stable `call_id` order.
- **2026-06-24** — Structured debug and error logging for the agent loop.
- **2026-06-23** — Auto-create session on first send and background final assistant write.
- **2026-06-26** — Tier 1–2 token optimization for context and tool output.
- **2026-06-28** — Repair broken tool turns in memory before provider replay.
- **2026-06-29** — Opt-in terse emission-style guidance in the harness prompt.
- **2026-07-01** — Opt-in session transcript capture for harness debugging (`GET` usage, SSE, usage DB).
- **2026-07-01** — Prompt-first scheduled loops: `start_loop` tool, scheduler worker, and Firestore-backed loop store.

#### Memory & context
- **2026-05-14** — Memory (`monkeybot.core.memory`): markdown files under `MEMORY_PATH` with optional `INDEX.md` snapshotted into context; `search_memory` for on-demand lookup; mid-turn refresh via `refresh_memory_index()`.
- **2026-05-14** — Memory organizer (`monkeybot.core.memory.organizer`) — async post-turn classifier that updates `INDEX.md` and routes new entries to the right markdown file.
- **2026-05-15** — Context curation configuration (`CONTEXT_CURATION_*` env vars) and optional secondary LLM curator pass.
- **2026-07-02** — Hybrid memory curation: append-only INDEX with archive cap, sliding `memory_window_lines`, structural coverage/confidence with `search_memory` nudge when truncated, optional LLM curator (`mode`: window/curator/hybrid), and index fingerprint cache to skip repeat curator calls.
- **2026-07-09** — Simplified context curation to a single path (recent window; curator only when token-heavy). Removed `CONTEXT_CURATION_MODE` / `window`/`curator`-only modes and dropped `memory_threshold`, `max_memory_lines`, and `search_max_hits` knobs (#67).
- **2026-07-02** — Skills discovered via `list_skills` instead of full prompt injection; skill names always injected from `ctx.skills`; `list_skills`/`read_file` remain the path for the skills root and full `SKILL.md` procedure.

#### Tools, skills & workspace
- **2026-05-14** — MCP (`mcp_client.py`, `ports_mcp.py`) — stdio + streamable HTTP MCP servers loaded from `MCP_CONFIG`; tools exposed as `server__tool`.
- **2026-05-16** — Web search integration and enhanced tool invocation.
- **2026-05-20** — MCP OAuth2 client credentials and password grant with strict validation.
- **2026-05-14** — Skills (`monkeybot.core.context._discover_skills`, `monkeybot.skills.{loader,executor}`) — folder-based skills under `SKILLS_PATH` with `SKILL.md` per skill.
- **2026-05-14** — Workspace tools (`workspace_tools.py`, `workspace_service.py`) — read/write/edit/find under a configurable workspace scope; paths under `.monkeybot` bypass `WORKSPACE_WRITE_SCOPE_REL`.
- **2026-06-24** — `render_image` tool, `ImageBlock` SSE events, and Gemini image-generation fixes.
- **2026-06-24** — Default `deny_patterns` block package installs via `run_command`.
- **2026-06-30** — Browser MCP integration (`integrations/browser-mcp/`) with tool result ingress guards.
- **2026-07-02** — YAML frontmatter `description` parsing for `list_skills`.

#### Providers
- **2026-05-14** — Provider adapters: Vertex / Gemini, OpenAI, Anthropic, Anthropic-on-Vertex. Resolved via `monkeybot.core.config.get_provider_config()`.
- **2026-05-18** — HuggingFace Inference API provider.
- **2026-05-18** — OpenTelemetry observability integration with runbook and configuration.
- **2026-06-22** — Provider prompt caching, cost telemetry, and playground model UI.
- **2026-06-30** — Ollama provider for local models.
- **2026-06-30** — Shared stream / `count_input_tokens` logic for OpenAI-compatible providers.
- **2026-07-01** — NVIDIA NIM provider (`monkeybot[nvidia]`).
- **2026-07-03** — Vertex `google_search` grounding for Gemini (agent turns only); CLI display for grounding metadata.

#### Storage & persistence
- **2026-05-17** — Storage abstraction layer (SQLite / Postgres, Step 1 + 1.5).
- **2026-05-17** — Pluggable memory storage (`WorkspaceStorage` + `MemorySubsystem`, Step 2).
- **2026-06-23** — Firestore backend, worker pool, and playground chat history.
- **2026-06-30** — Opt out of auto schema via `paths.auto_schema` in `monkeybot.yaml`.

#### CLI & developer experience
- **2026-06-24** — Standalone `monkeybot` CLI for agent setup and terminal chat (`monkeybot new`, `monkeybot chat`, `monkeybot run`).
- **2026-06-25** — Named subagent personas via `monkeybot.yaml` and `task(subagent_type)`.
- **2026-06-25** — `monkeybot` skill as a [skills.sh](https://skills.sh) entry point for onboarding.
- **2026-06-26** — Pinned session status bar in `monkeybot chat`.
- **2026-06-30** — Self-contained `demo_agent/` example (replaces playground) with its own `pyproject.toml` and editable path dependency.
- **2026-06-30** — `develop` → `main` release process and `scripts/release.py` helpers.
- **2026-07-01** — Chat UX polish and Ollama thinking-level control.

#### Evals & testing
- **2026-05-18** — Evals service, playground UI, and agent behavior tests.
- **2026-07-01** — Live eval PR scorecard: telemetry, requirement gates, smoke suite, and CI wiring.

#### Deployment & packaging
- **2026-05-17** — Public Docker artifacts (Step 3): `docker-compose.yml`, `docker/opensandbox.docker.toml`, `.env.example`, `docker/Dockerfile` with `HEALTHCHECK`.
- **2026-05-17** — Step 4 deployment guides (Pattern A, B, C).
- **2026-05-17** — Harness-as-library (Step 5) for embedding the runtime in other services.
- **2026-05-18** — OSS sanitization: gitignore `internal/`, GCP placeholders, public playground Docker.
- **2026-05-16** — Opt-in OpenSandbox execution backend.
- **2026-05-14** — Demo agent under `demo_agent/`, default skills under `.agents/skills/`.
- **2026-05-14** — Docs (v2): `docs/getting-started.md`, `docs/skills.md`; configuration reference in `.env.example`.
- **2026-05-14** — Test + bench infra: `tests/` (pytest + pytest-asyncio) and `testing/bench.py`.

#### Multimodal & UI
- **2026-06-22** — Multimodal attachments and refreshed playground chat UI.
- **2026-06-24** — Chat UI interleaves assistant prose with tool cards in the transcript.

### Changed

- **2026-05-14** — Packaging: `pyproject.toml` authors set to `human-plus-machine`; repository URLs point at `https://github.com/human-plus-machine/monkeybot`. Build backend is `hatchling`; package layout is `src/monkeybot`.
- **2026-05-14** — Default `SKILLS_PATH` is `./.agents/skills` (matches `.env.example` and `docker/Dockerfile`).
- **2026-05-16** — Core module restructure and configuration cleanup; legacy files removed.
- **2026-06-24** — Improved tool failure recovery and progress visibility in prompts.
- **2026-06-25** — Harness context budgeting and unified provider sampling parameters.
- **2026-06-26** — Gateway spawned from the agent project's Python env instead of the CLI env.
- **2026-06-30** — Scaffold consolidated packaged defaults; unified on `monkeybot new`.
- **2026-06-30** — Provider and runtime paths deduplicated; observability gap logging improved.
- **2026-07-02** — Pre-flight prompt tokens: summarization threshold and `estimated_prompt_tokens` use each provider's tokenizer / count API (`Provider.count_input_tokens`). OpenAI installs should include the `openai` extra (adds `tiktoken`).
- **2026-07-02** — Configurable history summarization model via `CONTEXT_SUMMARIZATION_MODEL`, optional `model.summarization_model` in `monkeybot.yaml`, or `TurnContext.summarization_model`; main turn still uses `ctx.model`.

### Fixed

- **2026-05-14** — Anthropic harness tool-call replay correctness.
- **2026-05-18** — Gateway resolves LLM providers via `get_provider_config`.
- **2026-06-23** — Task queue, worker claims, and session resume hardened.
- **2026-06-29** — Agent-root resolution, malformed tool args, and chat stream exit code.
- **2026-07-01** — OpenAI-compatible providers request stream usage metadata.
- **2026-07-03** — Skill names restored in prompt; session teardown on disconnect.
