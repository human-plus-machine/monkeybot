# monkeybot configuration reference

Every YAML option the harness reads, the values it accepts, and the default it uses when the key is omitted. Derived from the config loaders (`src/monkeybot/core/config/`, plus the tool, memory, and gateway code that reads each key). The commented template is `cli/src/monkeybot_cli/scaffold_defaults/monkeybot.example.yaml` (copied to `monkeybot_config/monkeybot.example.yaml` by `monkeybot new`). Where that template disagrees with the code, this file says so.

Secrets do not belong in these files. API keys and credential paths go in the agent root `.env`.

## How a file is loaded

1. Discovery. `MONKEYBOT_CONFIG` if it points at a file, otherwise the nearest parent directory that contains `monkeybot_config/monkeybot.yaml`.
2. `includes`. A top-level list of paths relative to the file that names them. Each fragment is deep-merged on top of the file so far. A fragment's own `includes` key is dropped, so includes are not recursive. A missing or unreadable fragment is skipped with a warning.
3. Precedence. For keys in `ENV_MAP` (`src/monkeybot/core/config/runtime_env.py`), a value already in the process environment wins over YAML. `.env` is loaded into the process environment first, so it wins too. `model.*` is the exception: those keys are YAML-only, leftover `MODEL_*` / `MAX_TURNS` variables are ignored, and load fails if `MODEL_PROVIDER` is set in the environment while `model.provider` is absent from YAML.
4. Paths. Relative `paths.*` values and a relative `sqlite:///` URL are anchored at the agent root (the directory that contains `monkeybot_config/`), not the process working directory.
5. `GOOGLE_CLOUD_PROJECT`, when set, replaces the YAML value of `gcp.project_id` and `anthropic_vertex.project_id`.
6. `WORKSPACE_ROOT`, when set, blocks copying `paths.workspace_root` into `MONKEYBOT_WORKSPACE_ROOT`. The path the process actually uses is still `paths.workspace_root`, unless the absolute `MONKEYBOT_WORKSPACE_ROOT_OVERRIDE` is set.

Reload tier is what a running gateway does when that key changes on disk:

| Tier | Meaning |
|---|---|
| hot | The next turn reads the new snapshot. |
| rebuild | The gateway swaps the affected slice (provider, inspectors, web search, memory hook, and similar). |
| reconnect_mcp | MCP clients reconnect. |
| restart | Not applied in process. Restart the gateway. |

YAML-only sections that are not in `ENV_MAP` (`subagents`, `verifier`, `coding`, and `memory.enabled`) are rebuild. `goals`, `pricing`, `includes`, and the direct-read keys (`runtime.transcript_enabled`, `paths.auto_schema`, `model.keep_alive`, `model.num_ctx`, `tools.read_max_lines`, `web_search.vertex_google_search`) have no tier: they are read from the file when the snapshot or that feature loads.

A YAML boolean is stored as the string `true` or `false` when it is copied to an environment variable. Readers that want a specific token are called out below.

## `model.provider`

Accepted YAML values. An alias is normalized before the provider is built.

| YAML value | Used as |
|---|---|
| `gemini`, `vertex` | `google_vertexai` (Vertex Gemini; ADC / `gcp.project_id`) |
| `google_vertexai` | `google_vertexai` |
| `google_genai` | `google_genai` (requires `GEMINI_API_KEY`) |
| `vertex-claude`, `vertex_claude` | `vertex_anthropic` (requires a GCP project; region from `anthropic_vertex.region`, default `us-east5`) |
| `vertex_anthropic` | `vertex_anthropic` |
| `openai` | `openai` (`OPENAI_API_KEY`) |
| `anthropic` | `anthropic` (`ANTHROPIC_API_KEY`) |
| `huggingface` | `huggingface` |
| `ollama` | `ollama` (auto local/cloud) |
| `ollama-cloud`, `ollama_cloud` | `ollama-cloud` |
| `ollama-local`, `ollama_local` | `ollama-local` |
| `nvidia` | `nvidia` (`NVIDIA_API_KEY`) |
| `openrouter` | `openrouter` |
| `fireworks` | `fireworks` (`FIREWORKS_API_KEY`) |
| `aws_bedrock` | `aws_bedrock` (`AWS_REGION` or `AWS_DEFAULT_REGION`) |
| `fake` | scripted test provider (`fake_provider.events_json`). Not for `get_provider_config`. |

Anything else fails validation. `azure_openai` is named in the error and is not supported.

Code default when `model.provider` is omitted: `google_vertexai`. The scaffold sets `gemini`.

## `monkeybot.yaml`

### `runtime`

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `runtime.log_level` | logging level name, case-insensitive (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) | `INFO` | `LOG_LEVEL` | hot | Process log level. `DEBUG` while troubleshooting. |
| `runtime.port` | integer | `8000` when both this and `gateway_port` are unset. Scaffold sets `8080`. | `PORT` | restart | Gateway listen port. |
| `runtime.gateway_port` | integer | unset; used only as a fallback when `PORT` is unset | `GATEWAY_PORT` | restart | Separate bind port for code that reads `GATEWAY_PORT`. |
| `runtime.transcript_enabled` | bool | `false`. Scaffold sets `true`. | YAML only (`MONKEYBOT_TRANSCRIPT_ENABLED` is ignored) | none | Write each session's events to `{workspace}/.monkeybot/transcripts/{UTC}_{session}/transcript.ndjson`. Not shown to the agent. |

### `harness`

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `harness.mode` | `turn_based`, `realtime` | `turn_based` | `MONKEYBOT_HARNESS_MODE` | restart | `turn_based` is HTTP `POST /reply` plus SSE. `realtime` is the WebSocket `/realtime` voice/text session and needs the `monkeybot[realtime]` extra. |

`realtime` is rejected unless `model.provider` is `gemini`, `vertex`, `google_vertexai`, or `google_genai`.

### `realtime`

Validated at startup even when `harness.mode` is `turn_based`. Takes effect only in realtime mode. Positive numbers are required when a key is set.

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `realtime.model.name` | string | unset; the session uses `model.name` | YAML only | none | Live-only model id, kept off the turn-based provider. |
| `realtime.model.provider` | provider id | unset; the session uses `model.provider` | YAML only | none | Provider for that live model. |
| `realtime.websocket.enabled` | bool | `true` | `MONKEYBOT_REALTIME_WS_ENABLED` | restart | Serve the realtime WebSocket. |
| `realtime.websocket.port` | integer | unset; uses `runtime.port` | `MONKEYBOT_REALTIME_WS_PORT` | restart | Bind port for the realtime socket. |
| `realtime.audio.input_format` | `pcm_s16le_24khz_mono`, `pcm_s16le_16khz_mono` | `pcm_s16le_24khz_mono` | `MONKEYBOT_REALTIME_AUDIO_INPUT_FORMAT` | restart | Microphone format. |
| `realtime.audio.output_format` | same two | `pcm_s16le_24khz_mono` | `MONKEYBOT_REALTIME_AUDIO_OUTPUT_FORMAT` | restart | Speaker format. |
| `realtime.audio.chunk_ms` | positive integer | `200` | `MONKEYBOT_REALTIME_AUDIO_CHUNK_MS` | restart | Audio chunk size. |
| `realtime.audio.max_utterance_sec` | positive number | `60` | `MONKEYBOT_REALTIME_AUDIO_MAX_UTTERANCE_SEC` | restart | Longest single utterance. |
| `realtime.session.max_duration_sec` | positive number | `1800` | `MONKEYBOT_REALTIME_SESSION_MAX_DURATION_SEC` | restart | Hard cap on one realtime session. |
| `realtime.session.idle_timeout_sec` | positive number | `120` | `MONKEYBOT_REALTIME_SESSION_IDLE_TIMEOUT_SEC` | restart | Close after this much silence. |
| `realtime.session.max_response_turn_sec` | positive number | `300` | `MONKEYBOT_REALTIME_SESSION_MAX_RESPONSE_TURN_SEC` | restart | Cap on one response turn. |
| `realtime.session.max_concurrent_sessions` | positive integer | `100` | `MONKEYBOT_REALTIME_SESSION_MAX_CONCURRENT_SESSIONS` | restart | Concurrent realtime sessions. |
| `realtime.metrics.emit_summary_on_close` | bool | `true` | `MONKEYBOT_REALTIME_METRICS_EMIT_SUMMARY_ON_CLOSE` | restart | Emit a metrics summary when a session closes. |

### `paths`

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `paths.agent_md` | path | `monkeybot_config/AGENT.md` | `AGENT_MD` | hot | System prompt file. |
| `paths.memory_storage_uri` | `local://` or `file://` path, or a bare relative path | `memory/mempalace` | `MEMORY_STORAGE_URI` | restart | MemPalace directory. `gcs://`, `gs://`, `s3://`, and any other non-local scheme log a warning and fall back to `local://<agent root>/memory/mempalace`. Validation still requires a GCP project when the raw value starts with `gcs://`. |
| `paths.memory_path` | path | unset; used only when `memory_storage_uri` is also unset | `MEMORY_PATH` | restart | Legacy location for the same directory. |
| `paths.skills_path` | path | `skills` | `SKILLS_PATH` | hot | Skill tree. Skill code runs with the agent, so the tree must be trusted. |
| `paths.db_url` | URL | `sqlite:///data/monkeybot.db` | `DB_URL` | restart | Conversation and scheduler storage. Use Postgres when subagents run in parallel; SQLite returns `database is locked`. |
| `paths.auto_schema` | bool | `true` | YAML only | none | Apply SQLite/Postgres DDL on open. Set `false` when migrations own the schema. |
| `paths.agent_id` | string | the agent root's filesystem path | `MONKEYBOT_AGENT_ID` | restart | Stable id for conversation history on a shared `db_url`. Set it when the agent is moved, remounted, or run as replicas, and do not change it afterwards. |
| `paths.mcp_config` | path | `monkeybot_config/mcp.json` | `MCP_CONFIG` | reconnect_mcp | MCP server definitions. |
| `paths.command_allowlist_config` | path | `monkeybot_config/command_allowlist.yaml` | `COMMAND_ALLOWLIST_CONFIG` | rebuild | `run_command` allowlist. See below. |
| `paths.permission_config` | path | `monkeybot_config/permissions.yaml` | `PERMISSION_CONFIG` | rebuild | Soft allow/ask/deny rules. See below. |
| `paths.approvals_config` | path | `monkeybot_config/approvals.json` | `MONKEYBOT_APPROVALS_CONFIG` | rebuild | Remembered approvals. Written by the app; do not hand-edit. |
| `paths.grants_config` | path | `monkeybot_config/grants.json`, or `<MONKEYBOT_APP_HOME>/grants.json` when that env var is set | `MONKEYBOT_GRANTS_CONFIG` | rebuild | Durable command and folder grants. |
| `paths.workspace_root` | path | `workspace` | `MONKEYBOT_WORKSPACE_ROOT` | restart | File-tool sandbox root. An absolute `MONKEYBOT_WORKSPACE_ROOT_OVERRIDE` wins over this key. |

### `model`

All of these are YAML-only. A value already in the environment does not override them. The ignored names are `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`, `MODEL_THINKING_BUDGET`, `MODEL_REASONING_EFFORT`, `MODEL_CONTEXT_WINDOW`, `MAX_TURNS`, `PROVIDER_MAX_RETRIES`, `STREAM_IDLE_TIMEOUT_S`, `MODEL_MAX_REQUEST_BYTES`, and `MODEL_CACHE_RETENTION`.

| Key | Type / choices | Default | Tier | Purpose |
|---|---|---|---|---|
| `model.provider` | see the provider table | `google_vertexai` (scaffold: `gemini`) | rebuild | Which LLM vendor. |
| `model.name` | model id string | `gemini-2.5-flash` (scaffold: `gemini-3.1-flash-live-preview`) | hot | Model id for the agent, history summarization, and chat titles. |
| `model.temperature` | number | `0.7` | rebuild | Sampling temperature. Lower is more deterministic. |
| `model.max_tokens` | integer | `60000` | rebuild | Cap on one response. |
| `model.thinking_budget` | integer | `-1` | rebuild | Gemini: `-1` model default, `0` off, `N` token budget. Ollama: `-1` server default, `0` off. Ignored when `reasoning_effort` is set on a provider that takes an effort level. |
| `model.reasoning_effort` | string, or YAML `off` / `on` | unset (provider default) | rebuild | Effort passed to the provider (`low`, `medium`, `high`, `xhigh`, `max`, or whatever that provider accepts). `off`, `none`, `disabled`, and an unquoted `off` (YAML boolean false) send `none`. An unquoted `on` (YAML boolean true) means the provider default. Gemini only sends `minimal`, `low`, `medium`, `high` and otherwise uses `thinking_budget`. A level the API rejects is omitted on later turns. Bedrock sends it only on the Anthropic model path; Converse models ignore it. Auxiliary calls (curator, summarizer) do not use the configured effort; OpenAI and Ollama send `none` for those. |
| `model.context_window` | positive integer tokens | `200000` (scaffold: `1000000`) | hot | Summarization trigger. Also sizes soft-spill and `read_file` character budgets. Not Ollama `num_ctx`. |
| `model.max_turns` | integer | `1000` | hot | Hard cap on turns in one run. |
| `model.provider_max_retries` | integer `>= 0` | `5` | hot | Retries for a transient provider failure. `0` disables retries. A non-integer is ignored and `5` is used. |
| `model.stream_idle_timeout_s` | number | `300` | hot | Seconds without a stream event before the call is treated as stalled. `0` or negative disables the timeout. |
| `model.max_request_bytes` | integer `>= 1` | unset: 32 MiB for OpenAI, Claude, and Vertex Claude; 25 MiB for Bedrock; 20 MiB for Gemini; 8 MiB otherwise | hot | Request body cap. Oversized media is stripped before send. |
| `model.cache_retention` | `none`, `short`, `long` | `short` (anything else is treated as `short`) | hot | Prompt-cache hint. Anthropic-family: `short` is 5 minutes, `long` is 1 hour, `none` sends no cache markers. OpenAI: `long` asks for 24h retention; `none` also skips the session cache key. |
| `model.keep_alive` | duration string | unset, and the Ollama provider then uses `24h`. `"0"` or `""` omits the field. | none | Ollama only. How long the local server keeps the model loaded. Not the daemon's `OLLAMA_KEEP_ALIVE`. A boolean is an error. |
| `model.num_ctx` | positive integer | unset (Ollama server default) | none | Ollama only. Do not copy `context_window` here. |

### `gcp` and `anthropic_vertex`

Non-secret identifiers. Prefer `.env` for credentials.

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `gcp.project_id` | string | unset | `VERTEX_AI_PROJECT_ID` (replaced by `GOOGLE_CLOUD_PROJECT` when that is set) | rebuild | Vertex Gemini project. |
| `gcp.location` | string | unset | `VERTEX_AI_LOCATION` | rebuild | Vertex Gemini location. Not read for `vertex_anthropic`; that provider warns and uses `anthropic_vertex.region`. |
| `anthropic_vertex.project_id` | string | unset | `ANTHROPIC_VERTEX_PROJECT_ID` (same `GOOGLE_CLOUD_PROJECT` override) | rebuild | Project for Vertex Claude. |
| `anthropic_vertex.region` | string | `us-east5` when the provider is `vertex_anthropic` and this is unset | `ANTHROPIC_VERTEX_REGION` | rebuild | Vertex Claude region. |

### `gateway`

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `gateway.pending_response_timeout_sec` | number | `300` | `PENDING_RESPONSE_TIMEOUT_SEC` | rebuild | How long a turn may stay pending. Raise it when long turns time out. |
| `gateway.sse_replay_max` | integer `>= 1` | `256` | `SSE_REPLAY_MAX` | restart | SSE frames kept for replay. |
| `gateway.sse_nested_replay_max` | integer `>= 1` | the `sse_replay_max` value | `SSE_NESTED_REPLAY_MAX` | restart | Separate replay lane for subagent traffic, so a chatty child cannot evict the parent. |
| `gateway.graceful_shutdown_timeout_sec` | integer | `5` | `GRACEFUL_SHUTDOWN_TIMEOUT_SEC` | restart | Drain time on shutdown. |
| `gateway.cors_allow_origins` | comma-separated origins, or `*` | `http://localhost:5173` | `MONKEYBOT_CORS_ALLOW_ORIGINS` | restart | Browser origins for the HTTP API. `*` allows any origin and disables credentials. Set this when a custom web UI is not on the Vite dev origin. |

### `memory`

MemPalace capture, wake-up, and search teaching. The vector stack is the `monkeybot[memory]` extra.

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `memory.enabled` | bool, or `1`/`true`/`yes`/`on`/`0`/`false`/`no`/`off` | `true` | YAML only. `MONKEYBOT_MEMORY_HOOK_ENABLED` is ignored | rebuild | Turn capture, wake-up, and prompt teaching off. Allowlisted shell commands can still read a palace on disk. |
| `memory.backend` | `chroma` only | `chroma` | `MEMPALACE_BACKEND` | restart | Local vector engine. Any other value fails validation. This is not the storage URI. |
| `memory.embedding_model` | string | `minilm` | `MEMPALACE_EMBEDDING_MODEL` | restart | Embedder. `embeddinggemma-300m` and `embeddinggemma_300m` are rewritten to `minilm` at startup. `embeddinggemma`, `embeddinggemma2`, and `openai-compat` are left as written. |

`memory_hook.enabled` is a legacy alias, read only when `memory.enabled` is absent.

Thread sharing is not a YAML key. `MONKEYBOT_MEMORY_SHARE_THREADS` defaults to sharing lines across chats in one project. `0`, `false`, `no`, and `off` keep each chat's lines in that chat.

### `emission`

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `emission.style` | `off` or `terse` | `off` | `MONKEYBOT_EMISSION_STYLE` | hot | `terse` (also `true`, `1`, `on`, `yes`) adds terse-output guidance to the cached prompt, and denser subagent handoffs when the `task` tool is active. Any other value is off. Leave `off` for a user-facing agent. |

### `subagents`

No environment override. A change rebuilds.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `subagents.timeout_sec` | number `>= 1` | `3600`. Scaffold sets `600`. | Per-subagent timeout. The parent can still cancel sooner. |
| `subagents.max_turns` | integer `>= 1` | `1000` | Per-subagent turn cap. |
| `subagents.vertex_google_search` | bool | `false` | Gemini only. Native `google_search` grounding for `task` runs. |
| `subagents.max_background` | integer `>= 1` | `4` | How many background subagents may run at once. |
| `subagents.lease_ms` | integer `>= 1000` | `60000` | Background subagent lease. |
| `subagents.personas` | list | none | Named types selected with `task(subagent_type=...)`. Duplicate `name` is an error. A bare list at `subagents:` is an error. `subagents.agent_md` was removed. |

Persona fields:

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | Value passed as `subagent_type`. |
| `description` | yes | Shown to the parent when it picks a type. |
| `agent_md` | no | Prompt file for this persona. `prompt_file` is accepted as an alias. Relative to the agent root. Without a `subagent_type`, the task inherits `paths.agent_md`. |
| `skills` | no | List of skill names. Default `[]`. |
| `model` | no | Model id override for this persona. |
| `vertex_location` | no | Vertex location override. `location` is accepted as an alias. |

### `tools`

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `tools.denied_patterns` | list of strings, or one string | none | `MONKEYBOT_TOOL_DENIED_PATTERNS` (comma-separated) | rebuild | Substrings blocked in tool arguments, for example `rm -rf`. Pair with `command_allowlist.yaml` for shell safety. |
| `tools.read_max_lines` | integer | `5000` | YAML only | none | Largest `limit` `read_file` will honor. |

`read_file` returns at most 2000 lines when `limit` is omitted. That 2000 is not configurable. Spill and read character budgets come from `model.context_window`. Context-pressure ratios are fixed in code.

### `compression`

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `compression.resume_thinking_budget` | integer | unset (keep the provider thinking budget) | `MONKEYBOT_RESUME_THINKING_BUDGET` | hot | Thinking budget used on a routine resume turn. Applied for `gemini`, `claude`, `ollama`, `ollama-cloud`, and `ollama-local` only. A non-integer is ignored. |

### `web_search`

| Key | Type / choices | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `web_search.backend` | `duckduckgo`, `tavily`, `firecrawl`, `none` | `duckduckgo` | `WEB_SEARCH_BACKEND` | rebuild | `none` removes the tool. `tavily` needs `TAVILY_API_KEY`. `firecrawl` needs `FIRECRAWL_API_KEY`. `duckduckgo` needs the `web-search` extra. |
| `web_search.max_results` | integer | `5` | `WEB_SEARCH_MAX_RESULTS` | rebuild | Result cap. |
| `web_search.vertex_google_search` | bool | `false` | YAML only | none | Gemini only, and additive to `backend`. Native `google_search` grounding on main-agent turns. Ignored for other providers, and not used for summarization, the memory organizer, or the curator. |

### `todo_list`

Parent agent only.

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `todo_list.enabled` | bool | `true` | `MONKEYBOT_TODO_LIST_ENABLED` | hot | Session todo list, injected under `## Todo list`. |
| `todo_list.mirror_to_disk` | bool | `true` | `MONKEYBOT_TODO_LIST_MIRROR_TO_DISK` | hot | Mirror `todos.json` under `.monkeybot/transcripts/{session}/`. |

### `computer`

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `computer.enabled` | bool | `false` | `MONKEYBOT_COMPUTER_TOOLS` | rebuild | macOS desktop only: `computer_*` tools (open files, launch apps, clipboard, trash). Leave `false` on a server. The desktop app sets the env var. Calls still ask for approval; "always allow" is per action, not per tool. |

### `sandbox`

OpenSandbox. `SANDBOX_ENABLED` must be the string `true` (any case). Only a YAML boolean `true` produces that. `run` and `chat` try to start the server and fail if Docker is down.

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `sandbox.enabled` | bool | `false` | `SANDBOX_ENABLED` | rebuild | Route `run_command` through a container. |
| `sandbox.server_url` | URL | `http://localhost:8080`. Scaffold sets `http://localhost:18080` so it does not collide with the gateway. | `SANDBOX_SERVER_URL` | rebuild | OpenSandbox endpoint. Must be `http(s)://host[:port]`. |
| `sandbox.image` | string | `python:3.12` | `SANDBOX_IMAGE` | rebuild | Container image. |
| `sandbox.ttl_seconds` | integer | `1800` | `SANDBOX_TTL_SECONDS` | rebuild | Sandbox lifetime. |
| `sandbox.shared_filesystem` | bool | `true` | `SANDBOX_SHARED_FILESYSTEM` | rebuild | `false` is for a remote server with no shared filesystem: compute-only commands, and workspace or skills file access is refused. |
| `sandbox.max_background_jobs` | integer `>= 1` | `4` | `SANDBOX_MAX_BACKGROUND_JOBS` | rebuild | Concurrent background commands. |
| `sandbox.max_job_seconds` | integer `>= 1` | `3300` | `SANDBOX_MAX_JOB_SECONDS` | rebuild | Longest background job. Longer work belongs on a broker. |
| `sandbox.renew_interval_seconds` | integer `>= 0` | `300` | `SANDBOX_RENEW_INTERVAL_SECONDS` | rebuild | How often a running job renews its worker. `0` renews once at start. |
| `sandbox.await_default_wait_seconds` | integer `>= 1` | `300` | `SANDBOX_AWAIT_DEFAULT_WAIT_SECONDS` | rebuild | Default wait for `await_command`. Clamped to the max. |
| `sandbox.await_max_wait_seconds` | integer `>= 1` | `600` | `SANDBOX_AWAIT_MAX_WAIT_SECONDS` | rebuild | Longest `await_command` wait. |
| `sandbox.prewarm` | bool | `true` | `SANDBOX_PREWARM` | rebuild | Prewarm the sandbox worker. |

`SANDBOX_API_KEY` (or `SANDBOX_AUTH_TOKEN`) and `SANDBOX_USE_SERVER_PROXY` (default `true`) are environment-only.

### `scheduler`

Loops are registered at runtime by the agent or the API. There is no static job list.

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `scheduler.enabled` | bool | `false`. On only when the value is `1`, `true`, `yes`, or `on`. | `MONKEYBOT_SCHEDULER_ENABLED` | rebuild | Run the tick worker inside the gateway. Production usually leaves this `false` and runs `python -m monkeybot.scheduler`. Needs `paths.db_url`. |

`goals.interval` shares the scheduler floor. These worker knobs are environment-only: `MONKEYBOT_SCHEDULER_MIN_INTERVAL_MS` (default `5000`), `MONKEYBOT_SCHEDULER_POLL_INTERVAL_S` (default `5`), `MONKEYBOT_SCHEDULER_CONCURRENCY` (default `1`), `MONKEYBOT_SCHEDULER_WORKER_ID`.

### `verifier`

YAML only. A change rebuilds. Realtime loops ignore this block. Off unless `verifier.enabled` is `true`. When the parent is on, an omitted `ledger.enabled` and `tracker.enabled` turn on, and an omitted `judge.enabled` turns on only when the tracker is on. An explicit `enabled: false` still opts that section out. An omitted `ledger.model` or `judge.model` uses `model.name`.

| Key | Type / choices | Default | Purpose |
|---|---|---|---|
| `verifier.enabled` | bool | `false` | Master switch. One ledger model call per human message. |
| `verifier.ledger.enabled` | bool | follows the parent | Goal-ledger classifier. |
| `verifier.ledger.model` | non-empty string | `model.name` | Ledger model. |
| `verifier.ledger.max_entries_per_thread` | integer `>= 1` | `64` | Ledger size cap. |
| `verifier.tracker.enabled` | bool | follows the parent | In-loop suspicion signals. |
| `verifier.tracker.suspicion_threshold` | integer `>= 1` | `3` | Signals before a verdict is considered. |
| `verifier.tracker.min_turn_before_verdict` | integer `>= 0` | `3` | Turns to wait before the first verdict. |
| `verifier.judge.enabled` | bool | parent on and tracker on | Async verdicts off the critical path. |
| `verifier.judge.model` | non-empty string | `model.name` | Judge model. |
| `verifier.judge.max_verdicts_per_message` | integer `>= 0` | `3` | Verdict cap per human message. |
| `verifier.judge.min_turns_between_verdicts` | integer `>= 0` | `2` | Minimum gap between verdicts. |
| `verifier.judge.max_spend_ratio` | number `>= 0` | `0.25` | Spend cap relative to the agent. |
| `verifier.judge.tail_grace_s` | number `>= 0` | `0` | How long the tail drain may wait. `0` does not block. |
| `verifier.judge.queue_cap` | integer `>= 1` | `32` | Jobs waiting to start. |
| `verifier.judge.max_in_flight` | integer `>= 1` | `8` | Verdicts running at once. |
| `verifier.escalation.max_severity` | `none`, `nudge`, `replan`, `steer`, `block` | `nudge` | Hardest intervention the verifier may use. `steer` is reserved. |

### `coding`

Coding mode is always on (read-before-edit, project instructions, git notes). This section only tunes it. YAML only. A change rebuilds.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `coding.verification_gate.enabled` | bool | `false` | Reject a final answer that follows edits the agent did not verify. One rejection per message by default. |
| `coding.verification_gate.verify_commands` | non-empty list of regex strings | `pytest`, `npm`/`pnpm`/`yarn test`, `cargo test`, `go test`, `make test` or `make check`, `ruff`, `mypy`, `tsc` | Replaces the built-in list. An invalid regex is an error. A background start, or a command that masks the exit (`pytest \|\| true`), does not count. |
| `coding.verification_gate.max_rejections` | integer `>= 0` | `1` | How many times the gate may reject one message. |
| `coding.project_instructions_files` | non-empty list of strings | `AGENTS.md`, `CLAUDE.md`, `.monkeybot/AGENTS.md` | First file found under `workspace_root` is injected into the prompt. |

### `goals`

YAML only. No reload tier; read when the snapshot is built. Omitted keys keep the defaults.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `goals.interval` | seconds as a number, or `20s`, `5m`, `1h` (units `s`/`sec`/`m`/`min`/`h`/`hr` and their longer forms) | `5m` | Tick interval. Must be at least `MONKEYBOT_SCHEDULER_MIN_INTERVAL_MS` (5000 ms unless overridden). |
| `goals.max_ticks` | integer `>= 1` | `50` | Tick budget. |
| `goals.max_runtime` | same duration syntax, or `off` | unset (no cap) | Wall-clock budget. |
| `goals.completion_judge.enabled` | bool | `true` | Block goal completion until a judge agrees. |
| `goals.completion_judge.model` | non-empty string | `model.name` | Judge model. |
| `goals.completion_judge.timeout_s` | number `>= 1` | `30` | Judge call timeout. |

### `pricing`

YAML only. USD per 1M tokens, keyed by model id. `input` and `output` are required. `cache_write` and `cache_read` default to `0`. `aliases` is a list of extra ids that share the rates. A repeated id is an error. Rates registered in process with `register_model_pricing` win over this section and over the built-in table. Subagent and scheduler workers are separate processes and only see this file.

```yaml
pricing:
  gemini-2.5-flash:
    input: 0.15
    output: 0.60
    cache_read: 0.03
    aliases: [gemini-flash]
```

### `fake_provider`

| Key | Type | Default | Env | Tier | Purpose |
|---|---|---|---|---|---|
| `fake_provider.events_json` | JSON string | unset | `MONKEYBOT_FAKE_PROVIDER_EVENTS` | rebuild | Scripted events for `model.provider: fake`. Tests only. |

### `includes`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `includes` | list of paths | none | Fragments merged on top of this file. Paths are relative to the file that lists them. Not recursive. |

## Constraints that span keys

- `harness.mode: realtime` requires a Gemini-family `model.provider` (`gemini`, `vertex`, `google_vertexai`, `google_genai`).
- `model.provider: vertex-claude` (and the other Vertex Claude ids) requires a GCP project from `gcp.project_id`, `anthropic_vertex.project_id`, or `GOOGLE_CLOUD_PROJECT` / `VERTEX_AI_PROJECT_ID` / `ANTHROPIC_VERTEX_PROJECT_ID`.
- A `paths.memory_storage_uri` that starts with `gcs://` fails validation without a GCP project, and the runtime then discards the object-store URI anyway (see `paths.memory_storage_uri`).
- `web_search.vertex_google_search` and `subagents.vertex_google_search` do nothing unless the provider is Gemini.
- Verifier nested `enabled` flags inherit from the parent as described in that section.

## Retired keys

Accepted so old files still load. Each logs a warning and is ignored.

| Key | Replacement |
|---|---|
| `tools.spill_min_chars`, `tools.spill_read_max_lines` | Spill size comes from `model.context_window`. |
| `tools.read_default_lines` | `read_file` defaults to 2000 lines. Pass `limit` to ask for more, up to `tools.read_max_lines`. |
| `runtime.transcript_include_live` | `runtime.transcript_enabled` is the only switch. |
| `coding.enabled` | Coding mode is always on. `coding.verification_gate.enabled` is the opt-in. |
| `model.summarization_model` | Summarization uses `model.name`. |
| `context_curation` | MemPalace wake-up replaced it. |
| `knowledge` | Removed. Agents search with `grep` and `glob`. |
| `subagents.agent_md` | Set `agent_md` on each persona. This one is an error, not a warning. |

## `command_allowlist.yaml`

Path: `paths.command_allowlist_config`. Hard gate on `run_command`, applied before the sandbox. `permissions.yaml` is a softer layer on top.

| Key | Type | Default when omitted | Purpose |
|---|---|---|---|
| `allowed_commands` | non-empty list of binary names | `cat`, `ls`, `grep`, `rg`, `echo`, `python`, `python3`, `uv`, `git`, `gh`, `bash`, `mempalace` | Binaries that may run. A binary outside this list and outside `grants.json` is asked, not run. `mempalace` is limited to the `search` subcommand. |
| `allowed_path_prefixes` | non-empty list of prefixes | `../memory/`, `../memory`, `./skills/`, `./skills`, `./global-skills/`, `./global-skills`, `./test-data/`, `./code/`, `./code` | Prefixes allowed in `./` and `/` arguments. This screens arguments. Filesystem isolation is what actually hides paths from a shell. |
| `deny_patterns` | list of regex strings | install blockers for pip, uv, poetry, conda, npm/pnpm/yarn, apt, and brew | Matched against the normalized command. Deny wins. An invalid regex is an error. The scaffold adds `curl`, `wget`, `rm -rf`, `sudo`, and writes to `/etc`. Those are not in the code default. |
| `tool_output` | map of tool name to a budget | built-in budgets below | Per-tool output shaping. Keys merge over the built-ins; omitted fields on a known tool keep the built-in value. |

`tool_output.<tool>` fields:

| Field | Type / choices | Purpose |
|---|---|---|
| `content_type` | `json`, `logs`, `code`, `prose`, `auto` | Shaper hint. |
| `max_output_lines` | positive integer | Line cap. Below 5, `monkeybot validate` warns. |
| `max_array_items` | positive integer | Array cap. Below 3, validate warns. |
| `keep_patterns` | list of regex strings | Lines that survive collapsing. |
| `collapse_repeated` | bool | Collapse repeated lines. Default `false`. |

Built-in budgets, used when `tool_output` is omitted or a field is left unset:

| Tool | Budget |
|---|---|
| `run_command` | `logs`, 400 lines, collapse repeats, keep lines matching error / fatal / traceback / exception |
| `web_search` | `json`, 30 array items |
| MCP tools | `auto`, 120 lines, 40 array items |

## `permissions.yaml`

Path: `paths.permission_config`. Last matching rule wins. Patterns are `fnmatch` (`*`, `?`) against the tool name and a resource string. The resource is the normalized `run_command` line, a `path` argument, a `computer_*` url or app, or the JSON arguments as a fallback.

| Key | Type / choices | Default | Purpose |
|---|---|---|---|
| `default` | `allow`, `ask`, `deny` | `allow` | Effect when no rule matches. |
| `rules` | list | `[]` | Ordered rules. |
| `rules[].tool` | non-empty string | required | Tool name pattern. |
| `rules[].pattern` | non-empty string | `*` | Resource pattern. |
| `rules[].effect` | `allow`, `ask`, `deny` | required | What to do on a match. |
| `rules[].message` | string | unset | Shown with `ask` or `deny`. |

An empty file, or a file that is only `null`, means allow everything that the hard allowlist already permits.

## `monkeybot validate` and `monkeybot doctor`

Config checks:

| Id | Command | What it checks |
|---|---|---|
| `config.file.exists` | validate | `monkeybot.yaml` was found. |
| `config.yaml.parses` | validate | The file parses. |
| `config.root.is_mapping` | validate | The root is a mapping. |
| `config.includes.resolve` | validate | Every `includes` entry is a file. |
| `model.provider.supported` | validate | `model.provider` is in the table above, and realtime mode has a Gemini provider. |
| `model.name.present` | validate | `model.name` is set. |
| `memory.backend.supported` | validate | `memory.backend` is `chroma` when set. |
| `gcp.project.required` | validate | A GCP project is present when a `gcs://` memory URI or Vertex Claude needs one. |
| `paths.agent_md.exists` | validate | `paths.agent_md` exists. |
| `paths.skills_path.exists` | validate | `paths.skills_path` exists. |
| `paths.mcp_config.exists` | validate | `paths.mcp_config` exists. |
| `paths.command_allowlist.exists` | validate | The allowlist file exists. |
| `paths.db_url.writable` | validate | The database location is writable. |
| `tool_output.parse` | validate | `tool_output` in the allowlist parses. |
| `tool_output.budgets.sane` | validate | Caps are not below the warning floors. |
| `mcp.json.parses` | validate | MCP config parses. |
| `mcp.servers.shape_valid` | validate | Each MCP server entry has the expected shape. |
| `mcp.env_refs.resolvable` | validate | `${ENV}` references in the MCP config resolve. |
| `runtime.port.free` | doctor | `runtime.port` is free. |
| `web_search.backend.ready` | doctor | The chosen backend's extra or API key is present. |
| `model.reasoning_effort` | doctor | The effort value is one this provider can send. |
| `ollama.local.num_ctx_invalid` | doctor | `model.num_ctx` is a positive integer. |
| `ollama.local.num_ctx_large` | doctor | `model.num_ctx` is not far above the machine. |
| `sandbox.status` | doctor | Sandbox mode and Docker reachability. |
| `provider.credentials.present` | doctor | The selected provider's credential is set. |
| `provider.extra.installed` | doctor | The provider extra is installed. |
| `gcp.adc.valid` | doctor | Application default credentials work. |

## Environment-only switches

Not YAML keys. Setting them in `.env` or the shell is the only way.

| Name | Default | Purpose |
|---|---|---|
| `MONKEYBOT_CONFIG` | discovery, see above | Pin the config file. |
| `MONKEYBOT_MEMORY_SHARE_THREADS` | share | `0` / `false` / `no` / `off` isolates memory per chat. |
| `MONKEYBOT_WORKSPACE_ROOT_OVERRIDE` | unset | Absolute workspace remap. Wins over `paths.workspace_root`. |
| `SANDBOX_API_KEY`, `SANDBOX_AUTH_TOKEN` | unset | OpenSandbox credential. |
| `SANDBOX_USE_SERVER_PROXY` | `true` | Route sandbox health and exec through the OpenSandbox HTTP API. |
| `SECRETS_PROVIDER` | `env` | `env` or `gcp_secret_manager`. Not read from YAML. `gcp_secret_manager` needs a GCP project. |
| `MONKEYBOT_SCHEDULER_MIN_INTERVAL_MS` | `5000` | Floor for `goals.interval` and loop intervals. |
| `MONKEYBOT_SCHEDULER_POLL_INTERVAL_S` | `5` | Tick worker poll. |
| `MONKEYBOT_SCHEDULER_CONCURRENCY` | `1` | Tick worker concurrency. |
| `MONKEYBOT_SCHEDULER_WORKER_ID` | a random id | Worker identity. |
