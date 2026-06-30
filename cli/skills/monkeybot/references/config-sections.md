# monkeybot.yaml configuration reference

Deep reference for every `monkeybot.yaml` section. Load this only when a user needs to customize beyond Tier 1. The canonical, fully-commented template lives at `src/monkeybot/monkeybot_config/monkeybot.example.yaml` in the monkeybot repo (copied to `monkeybot_config/monkeybot.example.yaml` when you scaffold); this file adds the **"when would I change this?"** context the comments don't.

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
| `memory_storage_uri` | `local://./data/memory` | `gcs://…` for shared/cloud memory (requires GCP project) |
| `skills_path` | `./skills` | Point at a different skills tree |
| `db_url` | `sqlite:///data/monkeybot.db` | **Postgres for parallel subagents** — SQLite hits `database is locked` under concurrency |
| `auto_schema` | `true` | Set `false` when migrations own the schema (managed Postgres with DML-only runtime user) |
| `mcp_config` | `./monkeybot_config/mcp.json` | Relocate MCP definitions |
| `command_allowlist_config` | `./monkeybot_config/command_allowlist.yaml` | Relocate the shell allowlist |
| `workspace_root` | `./workspace` (if present) | Change the file-tool sandbox root |

Validate check ids: `paths.agent_md.exists`, `paths.skills_path.exists`, `paths.mcp_config.exists`, `paths.command_allowlist.exists`, `paths.db_url.writable`, `memory.backend.supported`, `gcp.project.required` (for `gcs://` memory).

## `model`

| Field | Default | When to change |
|---|---|---|
| `provider` | `gemini` | Switch LLM vendor (see provider table in SKILL.md) |
| `name` | `gemini-3-flash` | Pick a specific model id |
| `temperature` | `0.7` | Lower for deterministic output, higher for creative |
| `max_tokens` | `60000` | Cap per-response length |
| `thinking_budget` | `-1` | Gemini reasoning budget: `-1` model default, `0` off, `N` token budget |
| `enable_caching` | `true` | Toggles explicit Anthropic prompt caching of the stable prefix |
| `context_window` | `1000000` | Summarization trigger threshold (tokens) |
| `max_turns` | `50` | Hard cap on turns per run |
| `summarization_model` | (main model) | Cheaper model for history summarization (env `CONTEXT_SUMMARIZATION_MODEL`) |

Validate check ids: `model.provider.supported`, `model.name.present`. Supported YAML providers: `gemini`/`vertex`, `openai`, `anthropic`, `vertex-claude`, `huggingface`, `aws_bedrock`, `fake`.

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

Required when `memory_storage_uri` is `gcs://…` or `provider: vertex-claude` (validate `gcp.project.required`).

## `gateway`

| Field | Default | When to change |
|---|---|---|
| `pending_response_timeout_sec` | `300` | Long-running turns time out too early |
| `sse_replay_max` | `256` | Tune SSE replay buffer |
| `graceful_shutdown_timeout_sec` | `5` | Allow longer drain on shutdown |
| `cors_allow_origins` | `http://localhost:5173` | **Custom web UI** — set its origin, or `"*"` for any |

## `context_curation`

Trims skills/memory injected into context. `enabled: true` by default.

| Field | Default | Notes |
|---|---|---|
| `skill_threshold` | `4` | Curate skills once this many are relevant |
| `memory_threshold` | `8` | Curate memory past this many lines |
| `curator_model` | `gemini-3-flash` | Separate small model; empty = main model |
| `timeout_sec` | `10` | Curator call timeout |
| `max_memory_lines` | `12` | Cap injected memory |
| `max_skills` | `5` | Cap injected skills |
| `search_max_hits` | `8` | Cap search hits considered |

Change when controlling cost or when too much/little context is being injected. Set `enabled: false` to skip entirely.

## `memory_hook`

`enabled: true` — automatic memory capture after turns. Disable to manage memory manually.

## `subagent` and `subagents`

`subagent` sets defaults for `task` calls:

| Field | Default | Notes |
|---|---|---|
| `timeout_sec` | `600` | Per-subagent timeout |
| `max_turns` | `25` | Per-subagent turn cap |
| `agent_md` | (parent `AGENT.md`) | Default prompt when `task` omits `subagent_type` |

`subagents[]` defines named personas the parent selects via `task(subagent_type=...)`:

```yaml
subagents:
  - name: researcher
    description: "Deep-dives a topic and returns a structured summary."
    agent_md: ./monkeybot_config/agents/researcher.md
```

Relative paths resolve from the bot project root, not `workspace/`. For parallel fan-out, use Postgres (`db_url`).

## `tools`

| Field | Default | When to change |
|---|---|---|
| `denied_patterns` | (none) | Block substrings in tool args, e.g. `"rm -rf"` (also env `MONKEYBOT_TOOL_DENIED_PATTERNS`) |
| `read_max_lines` / `read_default_lines` | (code defaults) | Tune file-read limits |
| `spill_read_max_lines` / `spill_min_chars` | (code defaults) | Tune large-result spill behavior |
| `result_budget_fraction` / `result_budget_floor_tokens` | (code defaults) | Advanced result-budgeting; rarely needed |

For shell-command safety, pair `denied_patterns` with `monkeybot_config/command_allowlist.yaml`.

## `web_search`

| Field | Default | Notes |
|---|---|---|
| `backend` | `duckduckgo` | `duckduckgo` (no key) \| `tavily` \| `firecrawl` \| `none` |
| `max_results` | `5` | Result cap |

Tavily/Firecrawl need `TAVILY_API_KEY` / `FIRECRAWL_API_KEY` in `.env`. Doctor check: `web_search.backend.ready` (`duckduckgo` needs the `web-search` extra).

## `sandbox`

| Field | Default | When to change |
|---|---|---|
| `enabled` | `false` | Enable to run untrusted code in isolation |
| `server_url` | `http://localhost:8080` | Sandbox service endpoint |
| `image` | `python:3.12` | Execution image |
| `ttl_seconds` | `1800` | Sandbox lifetime |

Needs `SANDBOX_API_KEY` in `.env`.

## `includes`

Top-level list of YAML fragments (paths relative to the config file's directory). Later files deep-merge over earlier ones — useful for per-environment overrides:

```yaml
includes:
  - includes/local.yaml
```

Validate check: `config.includes.resolve`.

## `fake_provider`

Test-only. `events_json` feeds `MODEL_PROVIDER=fake` scripted runs (env `MONKEYBOT_FAKE_PROVIDER_EVENTS`). Not for production.
