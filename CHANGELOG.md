# Changelog

All notable changes to monkeybot are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Core

### Fixed

- Host `run_command` no longer fail-closes every command when filesystem isolation is unavailable and none of the memory hidden paths exist on disk (typical in nested Linux containers with memory off). Isolation is still required — and the command still refused — when at least one palace path is present.

## [core v3.0.0] - 2026-08-17

### Breaking

- Note-based memory (INDEX.md, curator, `search_memory` / `edit_memory` / `forget`) is replaced by per-agent MemPalace with a durable outbox on SQLite, Postgres, and Firestore. Capture can be turned off with `memory.enabled: false` or `MONKEYBOT_MEMORY_HOOK_ENABLED=0`. Object-store palace URIs (`gcs://`, `s3://`) are not supported; use `local://`. MemPalace itself is the optional `monkeybot[memory]` extra.
- `subagents:` as a bare list of personas is no longer supported; wrap personas under `subagents.personas` in `monkeybot.yaml`. `SUBAGENT_TIMEOUT_SEC`, `SUBAGENT_MAX_TURNS`, and `MONKEYBOT_SUBAGENT_AGENT_MD` environment variable overrides are removed — set `subagents.timeout_sec`, `subagents.max_turns`, and per-persona `agent_md` in `monkeybot.yaml` instead. Top-level `subagents.agent_md` is also removed.

### Added

- MemPalace outbox now persists on Postgres and Firestore (same table/document shape as SQLite), with replica `palace_id` claim partitioning. Replicated deployments must share a lock-capable palace volume.
- Knowledge indexing follows document structure instead of fixed line windows: markdown headings, code definitions (tree-sitter via the optional `knowledge-ast` extra, brace/indent heuristic otherwise), and JSON/YAML/TOML top-level keys become chunk boundaries.
- Embeddings run on NVIDIA, OpenAI, any OpenAI-compatible endpoint, Voyage, or Gemini via `knowledge.embeddings.provider`. Misconfigured or unavailable providers degrade to keyword + graph search rather than failing the turn.
- PDF, DOCX, and image files are indexed with the `knowledge-media` extra. Images use `knowledge.captions` (`off` / `path` / `llm`); DOCX indexing covers tables as well as paragraphs.
- Subagents search the parent workspace index read-only, and a second gateway attempting to write the same index is refused.
- Soft spill and unified `read_file` char budgets derive from `model.context_window`: large tool results always land on disk with a large inline body when headroom allows; `read_file` returns `next_offset` and never lies about `end_line`.
- When memory is off, host `run_command` children cannot see palace files (Linux user+mount namespaces or macOS `sandbox-exec`). If isolation cannot be established, the command is refused. OpenSandbox does not mount the palace. `/tmp` and `/var/folders` are no longer implicitly allowlisted path prefixes.

### Fixed

- Streamable HTTP MCP connects accept both 2-tuple and 3-tuple transport yields from the MCP Python SDK.
- Stop mid-reply now cancels the in-flight provider token stream (instead of waiting for the full LLM call) and persists any already-streamed assistant text to history so follow-up turns keep matching what the user saw.
- Chunking improvements now reach existing workspaces: a chunker version bump re-chunks indexed files even when their modification time never changed, so upgrades no longer require deleting `.monkeybot/knowledge/`.
- Vector search scores only vectors from the active embedding model. Switching provider or `dimensions` purges the incomparable rows at startup and re-embeds them, instead of blending two models into one similarity ranking.
- The knowledge index writer lock is claimed atomically, so two gateways starting at the same moment can no longer both believe they own the index.
- Embedding requests carry a per-request timeout, so one slow endpoint cannot stall an indexing pass; cached vector matrices are bounded by a memory budget with least-recently-used eviction.
- PDF extraction closes its file handle (previously leaked a descriptor per file on large scans), the code chunker no longer splits mid-function when a docstring contains an unbalanced brace, and `knowledge.chunk_overlap_ratio` is honored for markdown, code, and structured files.
- `read_file` no longer applies a post-hoc 32k char chop after line selection (which made `end_line` lie and caused the model to skip unread lines when paging).
- `read_file` reports `truncated: false` and omits `next_offset` when a read reaches the end of the file, instead of marking every complete read truncated and pointing past EOF. A line too long for the whole char budget is now marked inline where it was cut.
- Tool results too large to inline as-is are shaped into still-valid JSON (with `… (+N more items)` markers) rather than inlined as a raw, unparseable JSON prefix.
- Context summarization sizes its per-result cap from the active model's context window rather than a hardcoded 200k window.

### Changed

- `mcp` is pinned to `>=1.0.0,<2` until Streamable HTTP can construct an `httpx2.AsyncClient` for MCP SDK 2.x (see #190).
- Subagent defaults and named personas are now configured under a single `subagents:` YAML mapping (`subagents.timeout_sec`, `subagents.max_turns`, `subagents.vertex_google_search`, `subagents.personas`), replacing the separate `subagent:` defaults block and bare-list `subagents:` personas. Persona prompts live only on `subagents.personas[].agent_md`; tasks without a `subagent_type` inherit the parent `paths.agent_md`.
- Spill sizing is window-derived (soft spill). `tools.spill_min_chars` / `tools.spill_read_max_lines` / `tools.read_default_lines` are retired (warned, ignored). `tools.read_max_lines` is YAML-only (env overrides removed). `read_file` defaults to 2000 lines when `limit` is omitted; pass `limit` to request more. Large ordinary reads can return more content than the old flat 32k cap.

## [browser v0.3.0] - 2026-08-17

### Added

- AWS Bedrock AgentCore Browser backend via the optional `agentcore` extra (`bedrock-agentcore`, Playwright).
- Prefer a Monkeyapp in-app CDP endpoint over desktop Chrome or a stale `BROWSER_CDP_*` env when the in-app CDP file is present.

### Changed

- `mcp` is pinned to `>=1.0.0,<2` (same Streamable HTTP constraint as core; see #190).

## [cli v0.5.0] - 2026-08-17

### Added

- `monkeybot run` / `chat` / `talk` refuse to start when the agent interpreter lacks MonkeyBot 3.x (and MemPalace when memory is enabled). A failed probe may `uv sync` an existing lock; it never rewrites `pyproject.toml`. Config-only agents with memory on get a CLI-managed cache venv holding `monkeybot[memory]` pinned to the running core, reused offline. A local monkeybot checkout can provision that runtime from source; the cache invalidates when those sources change.
- `monkeybot chat` TUI gained Claude-Code-style interaction: `Esc` interrupts the active turn (double-tap while idle recalls your last message for editing), `Shift+Tab` cycles a client-side approval mode (`normal` / `auto-approve` / `deny-confirms` — auto-answers tool confirmation prompts only, elicitations still ask), `@` fuzzy-inserts a workspace file path, `!<command>` runs a local shell command shown in the transcript but never sent to the agent, and `?` opens a keyboard-shortcut overlay. New slash commands: `/clear` (alias of `/new`), `/model` (switches model by starting a fresh session), `/status`, `/config`.
- `monkeybot chat -c` / `--continue` resumes the most recent session for the current agent.

### Changed

- `monkeybot new` no longer scaffolds capability skills into new agents.
- Declares `monkeybot[cli]>=3.0.0,<4` so a global CLI install pulls MonkeyBot 3.x.

## [cli v0.3.1] - 2026-07-28

### Changed

- Scaffold `monkeybot.example.yaml` documents harness-fixed `read_file` default (2000 lines), YAML-only `tools.read_max_lines`, and retired `read_default_lines` / spill knobs. `model.context_window` notes that it also drives soft-spill / read char budgets.

## [core v2.2.0] - 2026-07-13

### Added

- Canonical agent-root layout and a read-only virtual `skills/` root.

### Changed

- Remote sandboxes require an explicit compute-only capability setting.

### Breaking

- Workspace or skills symlinks that resolve outside their respective roots are now rejected. Move their targets inside the agent's `workspace/`, `data/`, or `skills/` layout.

## [browser v0.2.0] - 2026-07-13

### Added

- Browser MCP package release supporting the canonical agent layout.

## [cli v0.3.0] - 2026-07-13

### Added

- Agent scaffolds now include the canonical layout, Dockerfile, and disabled browser MCP configuration.

### Fixed

- `monkeybot chat` no longer adopts a stale gateway on a busy port: it refuses to spawn when the port is already in use (suggesting `/bye`, stopping `monkeybot run`, or `--attach`), and `wait_for_health` rejects a `/health` 200 if the child process has already exited.

## [0.2.1] - 2026-07-13

### Added

- First PyPI release of **monkeybot-cli** (Trusted Publishing) — install with `uv tool install monkeybot-cli`.


## [2.1.1] - 2026-07-13

### Added

- Evals: `response_regex` and `response_not_contains` deterministic assertions; run files now persist per-turn prompt/response text, and `python -m evals.diff` pinpoints the exact turn behind a regression between two runs.
- Live eval smoke workflow now also runs on every push to `main` (report-only, scorecard on the run Summary page) and on manual dispatch, in addition to the existing PR gates.
- Live evals need only `NVIDIA_API_KEY`: the deepeval judge can now run on build.nvidia.com models (`JUDGE_PROVIDER=nvidia`) via NVIDIA's OpenAI-compatible endpoint, and CI pins agent (`meta/llama-3.3-70b-instruct`) and judge (`nvidia/llama-3.3-nemotron-super-49b-v1`) to two different free NVIDIA-hosted models. The workflow also now sets `MODEL_PROVIDER`/`MODEL_NAME` explicitly — previously it booted whatever `demo_agent`'s committed config said (ollama, which doesn't exist in CI).
- Repo-root `monkeybot_config_example/` — full-option human-readable config templates (`.example` filenames).
- `monkeybot new` also scaffolds `permissions.yaml`.
- `monkeybot new` writes an agent-project `pyproject.toml` with a PyPI `monkeybot[<provider>]>=2.1.0,<3` dependency (plain `uv sync` after scaffold).
- `monkeybot new` interactive menus (and `--with`) select additional providers/features into that same `monkeybot[…]` dep list.
- `scripts/smoke_global_cli.sh` — local-wheel stand-in for clean-machine PyPI smoke (`uv tool install` → `new` → `uv sync` → `validate`/`doctor`/`chat`).
- README / getting-started / onboarding skill lead with `uv tool install monkeybot-cli` (clone is contributor-only).
- **monkeybot-cli** declares published core bound `monkeybot[cli]>=2.1.0,<3` (local clones still use `[tool.uv.sources]`).

### Removed

- Evals FastAPI service (`evals/main.py`, in-memory store, WebSocket fan-out, Dockerfile, `docker-compose.evals.yml`) — nothing consumed its HTTP/WS API; `python -m evals.report` is the single execution path. `google-genai` (Gemini judge dep) moved into the root `evals` extra.

### Changed

- `monkeybot doctor` remediation for missing extras: add `monkeybot[<extra>]` to agent deps + `uv sync` (keeps `uv sync --extra` only when the agent defines project-level optionals).
- `fake` provider credentials treated as optional in `doctor` (smoke / no-API-key path).
- `publish-release.yml` Trusted-Publishes to PyPI (OIDC) packages tagged in that run only — core before cli; `scripts/release.py publish` emits `packages` via `GITHUB_OUTPUT`.
- Remove redundant CLI hatch `force-include` of `scaffold_defaults` (duplicated wheel paths and broke `uv build`).
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
