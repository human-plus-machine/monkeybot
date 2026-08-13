# monkeybot.yaml configuration reference

Deep reference for every `monkeybot.yaml` section. Load this only when a user needs to customize beyond Tier 1. The canonical, fully-commented template lives at `cli/src/monkeybot_cli/scaffold_defaults/monkeybot.example.yaml` in the monkeybot repo (copied to `monkeybot_config/monkeybot.example.yaml` when you scaffold); this file adds the **"when would I change this?"** context the comments don't.

**Precedence:** env vars and `.env` win over YAML. The YAML→env mapping is `ENV_MAP` in `src/monkeybot/core/config/runtime_env.py`. If a YAML edit has no effect, check for a shadowing env var.

---

## `runtime`

| Field | Default | When to change |
|---|---|---|
| `log_level` | `INFO` | `DEBUG` while troubleshooting; `WARNING`/`ERROR` to quiet logs |
| `port` | `8080` | Port conflict, or running multiple bots locally |
| `gateway_port` | (unset) | Only if a code path needs `GATEWAY_PORT` separate from `PORT` |

Validate/doctor: `doctor` → `runtime.port.free`.

## `paths`

| Field | Default | When to change |
|---|---|---|
| `agent_md` | `./monkeybot_config/AGENT.md` | Alternate system-prompt location |
| `memory_storage_uri` | `local://./memory/mempalace` | Local MemPalace root. Cloud object-store URIs (`gcs://`, `s3://`) are not supported. |
| `skills_path` | `./skills` | Point at a different skills tree |
| `db_url` | `sqlite:///data/monkeybot.db` | **Postgres for parallel subagents** — SQLite hits `database is locked` under concurrency |
| `auto_schema` | `true` | Set `false` when migrations own the schema (managed Postgres with DML-only runtime user) |
| `mcp_config` | `./monkeybot_config/mcp.json` | Relocate MCP definitions |
| `command_allowlist_config` | `./monkeybot_config/command_allowlist.yaml` | Relocate the shell allowlist |
| `workspace_root` | `./workspace` (if present) | Change the file-tool sandbox root |

Validate check ids: `paths.agent_md.exists`, `paths.skills_path.exists`, `paths.mcp_config.exists`, `paths.command_allowlist.exists`, `paths.db_url.writable`, `memory.backend.supported`.

## `model`

| Field | Default | When to change |
|---|---|---|
| `provider` | `gemini` | Switch LLM vendor (see provider table in SKILL.md) |
| `name` | `gemini-3-flash` | Pick a specific model id |
| `temperature` | `0.7` | Lower for deterministic output, higher for creative |
| `max_tokens` | `60000` | Cap per-response length |
| `thinking_budget` | `-1` | Gemini: `-1` model default, `0` off, `N` token budget. Ollama reasoning models: `-1` server default, `0` off (`reasoning_effort: none`) |
| `context_window` | `1000000` | Summarization trigger (tokens); also drives soft-spill / `read_file` char budgets |
| `max_turns` | `1000` | Hard cap on turns per run |
| `summarization_model` | (main model) | Cheaper model for history summarization (env `CONTEXT_SUMMARIZATION_MODEL`) |

Validate check ids: `model.provider.supported`, `model.name.present`. Supported YAML providers: `gemini`/`vertex`, `openai`, `anthropic`, `vertex-claude`, `huggingface`, `ollama`, `aws_bedrock`, `fake`.

## `gcp` / `anthropic_vertex` (non-secret identifiers)

Prefer `.env` for secrets and the ADC path; use these blocks only for non-secret project/region identifiers.

```yaml
gcp:
  project_id: your-gcp-project
  location: us-central1
anthropic_vertex:
  project_id: your-gcp-project
  region: us-east5
```

Required when `provider: vertex-claude` (validate `gcp.project.required`).

## `gateway`

| Field | Default | When to change |
|---|---|---|
| `pending_response_timeout_sec` | `300` | Long-running turns time out too early |
| `sse_replay_max` | `256` | Tune SSE replay buffer |
| `graceful_shutdown_timeout_sec` | `5` | Allow longer drain on shutdown |
| `cors_allow_origins` | `http://localhost:5173` | **Custom web UI** — set its origin, or `"*"` for any |

## `context_curation`

Trims memory injected into context. `enabled: true` by default.

Recent window by default; LLM curator only when the index is token-heavy. On curator failure, falls back to the window.

| Field | Default | Notes |
|---|---|---|
| `memory_window_lines` | `12` | Recent index lines injected; also caps curator-selected lines |
| `memory_index_cap` | `200` | Organizer keeps this many INDEX.md entries; older rows move to `INDEX.archive.md` |
| `memory_token_threshold` | `2000` | Call curator when estimated index tokens exceed this |
| `curator_model` | `gemini-3-flash` | Separate small model; empty = main model |
| `timeout_sec` | `10` | Curator call timeout |

When the prompt shows fewer entries than exist, a structural confidence score triggers a `search_memory` nudge. Skill names are always shown in full in the prompt; use `list_skills` to get the skills root path.

## `memory`

MemPalace capture and recall are on by default. Set `enabled: false` to opt out (no ingest, no wake-up, no `mempalace search` teaching). Legacy `memory_hook.enabled` and `MONKEYBOT_MEMORY_HOOK_ENABLED` still work; the env var wins over YAML.

| Field | Default | When to change |
|---|---|---|
| `enabled` | `true` | Set `false` to disable capture/recall for privacy or retention |
| `engine` | `mempalace` | Leave as-is |
| `backend` | `chroma` | Alternate MemPalace vector backend |
| `embedding_model` | `embeddinggemma-300m` | Match the embedder the palace was built with |

## `subagents`

Global defaults for `task` calls, plus optional named personas:

| Field | Default | Notes |
|---|---|---|
| `timeout_sec` | `600` | Per-subagent timeout |
| `max_turns` | `1000` | Per-subagent turn cap |
| `vertex_google_search` | `false` | **Gemini only.** Enables native `google_search` grounding for subagent `task` runs. Config-file only. |
| `personas` | (none) | Named types selected via `task(subagent_type=...)`; each persona sets its own `agent_md` |

```yaml
subagents:
  timeout_sec: 600
  max_turns: 1000
  vertex_google_search: false
  personas:
    - name: researcher
      description: "Deep-dives a topic and returns a structured summary."
      agent_md: ./monkeybot_config/agents/researcher.md
```

Without a `subagent_type`, the task inherits the parent `AGENT.md` (`paths.agent_md`). Relative paths resolve from the bot project root, not `workspace/`. For parallel fan-out, use Postgres (`db_url`).

## `tools`

| Field | Default | When to change |
|---|---|---|
| `denied_patterns` | (none) | Block substrings in tool args, e.g. `"rm -rf"` (also env `MONKEYBOT_TOOL_DENIED_PATTERNS`) |
| `read_max_lines` | `5000` | Cap on `read_file` `limit` (**YAML only** — no env override). Default when `limit` is omitted is harness-fixed at **2000** (pass `limit` to request more). |

Spill and `read_file` char budgets are derived from `model.context_window` (retired keys `spill_min_chars` / `spill_read_max_lines` / `read_default_lines` warn and are ignored). Context pressure ratios and tool-result budget fractions are fixed in harness code (not YAML/env).

For shell-command safety, pair `denied_patterns` with `monkeybot_config/command_allowlist.yaml`.

## `web_search`

| Field | Default | Notes |
|---|---|---|
| `backend` | `duckduckgo` | `duckduckgo` (no key) \| `tavily` \| `firecrawl` \| `none` |
| `max_results` | `5` | Result cap |
| `vertex_google_search` | `false` | **Gemini only.** Additive to `backend` — enables Vertex Gemini's native `google_search` grounding tool for **main agent** turns only (not summarization, memory organizer, or curator). Ignored for other model providers. Config-file only — no env var override (like `paths.auto_schema`). |

Tavily/Firecrawl need `TAVILY_API_KEY` / `FIRECRAWL_API_KEY` in `.env`. Doctor check: `web_search.backend.ready` (`duckduckgo` needs the `web-search` extra).

## `sandbox`

| Field | Default | When to change |
|---|---|---|
| `enabled` | `false` | Enable to run untrusted code in isolation |
| `server_url` | `http://localhost:8080` | Sandbox service endpoint |
| `image` | `python:3.12` | Execution image |
| `ttl_seconds` | `1800` | Sandbox lifetime |

Needs `SANDBOX_API_KEY` in `.env`.

## `scheduler`

Prompt-first scheduled loops (`start_loop`, `/scheduler/loops`). Requires durable storage (`paths.db_url`). Loops are registered at runtime — there is no static `jobs` list in YAML.

| Field | Default | When to change |
|---|---|---|
| `enabled` | `false` | `true` runs the tick worker in-process on the gateway (local/dev). Production: leave `false` and run `python -m monkeybot.scheduler` as a separate process |

Env override: `MONKEYBOT_SCHEDULER_ENABLED` (`1` \| `true` \| `yes` \| `on`).

## `includes`

Top-level list of YAML fragments (paths relative to the config file's directory). Later files deep-merge over earlier ones — useful for per-environment overrides:

```yaml
includes:
  - includes/local.yaml
```

Validate check: `config.includes.resolve`.

## `fake_provider`

Test-only. `events_json` feeds `MODEL_PROVIDER=fake` scripted runs (env `MONKEYBOT_FAKE_PROVIDER_EVENTS`). Not for production.
