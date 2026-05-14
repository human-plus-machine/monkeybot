# Design: monkeybot-v2-e2 — Safety, Skills & Production Gateway
## Phase 1B: Detailed Contracts

**Date:** 2026-05-13  
**Status:** Phase 1B — API Contracts & Integration Points

---

## Common Patterns

### Error Handling

Errors in tools and providers are caught by `AgentLoop` and emitted as `ErrorEvent`. The gateway catches unhandled exceptions and returns HTTP `500`. Platform-specific error responses (e.g. Slack's `{"text": "..."}` error format) are handled by the user's `format_response()` in `webhook.py`.

**HTTP error responses from `WebhookGateway`:**

| Status | When |
|--------|------|
| `200` | Successful agent response (even if agent returned an error message) |
| `400` | `extract_message()` returned `None` and event type is unrecognised |
| `401` | `WEBHOOK_SECRET` is set and token verification failed |
| `422` | FastAPI body parse error (non-JSON payload) |
| `500` | Unhandled exception in gateway handler |

**Response body:** FastAPI default `{"detail": "..."}` for errors.

### Environment Variables

| Variable | Required | Default | Used By |
|----------|----------|---------|---------|
| `GEMINI_API_KEY` | If `MODEL_PROVIDER=gemini` | — | `GeminiProvider` |
| `ANTHROPIC_API_KEY` | If `MODEL_PROVIDER=claude` | — | `ClaudeProvider` |
| `MODEL_PROVIDER` | No | `"gemini"` | Provider factory in `cli.py` |
| `WEBHOOK_SECRET` | No | `""` | `WebhookGateway` token verification |
| `DB_URL` | No | `"sqlite:///data/monkeybot.db"` | `ConversationHistory` |
| `MEMORY_PATH` | No | `"./data/memory"` | `AgentLoop` config |
| `SKILLS_PATH` | No | `"./.agents/skills"` | `AgentLoop` config |
| `LOG_LEVEL` | No | `"INFO"` | Logging setup |
| `HOST` | No | `"0.0.0.0"` | `monkeybot serve` |
| `PORT` | No | `"8080"` | `monkeybot serve` |

---

## Module Contracts

### 1. `providers/_utils.py` — Shared Cost Utility

```python
def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. pricing values are (input_$/M, output_$/M)."""
    rates = pricing.get(model, (0.0, 0.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
```

`GeminiProvider` updated to import and call this instead of its local `_estimate_cost()`. `ClaudeProvider` imports the same function. No other changes to `GeminiProvider`.

---

### 2. `providers/claude.py` — Full ClaudeProvider

#### Class contract

```python
class ClaudeProvider:
    name: str = "claude"
    supports_streaming: bool = True

    def __init__(self) -> None:
        # Fail-fast — validate key at construction, not on first API call
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Install: uv pip install 'monkeybot[claude]' "
                "then set ANTHROPIC_API_KEY."
            )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str = "claude-3-5-sonnet-20241022",
        system: str = "",
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]: ...
```

#### Message conversion — Anthropic wire format

| `Message.role` | Anthropic format |
|----------------|-----------------|
| `"user"` | `{"role": "user", "content": str}` |
| `"assistant"` (text) | `{"role": "assistant", "content": str}` |
| `"assistant"` (tool call) | `{"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name, "input": args}]}` |
| `"tool"` (result) | `{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result}]}` |

The `AgentLoop` already appends `role="assistant"` (with `tool_call_id`) then `role="tool"` in order. The converter groups these pairs into the correct Anthropic block structure.

#### Tool definition conversion

```python
# ToolDef → Anthropic tool dict
{"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
```

`tool.parameters` is a JSON Schema dict — same shape Anthropic's `input_schema` expects.

#### Streaming event mapping

| SDK event | Action |
|-----------|--------|
| `RawMessageStartEvent` | Capture `usage.input_tokens` |
| `RawContentBlockStartEvent` (type=`text`) | Begin text accumulation |
| `RawContentBlockDeltaEvent` (type=`text_delta`) | `yield TextDelta(delta.text)` |
| `RawContentBlockStartEvent` (type=`tool_use`) | Record `id`, `name`; begin JSON accumulation |
| `RawContentBlockDeltaEvent` (type=`input_json_delta`) | Accumulate `delta.partial_json` |
| `RawContentBlockStopEvent` (tool_use active) | `json.loads(accumulated)` → `yield ToolCall(call_id, name, args)` |
| `RawMessageDeltaEvent` | Capture `usage.output_tokens` |
| After stream | `yield ProviderDone(ProviderUsage(input_tokens, output_tokens, cost_usd))` |

#### Pricing table

```python
_PRICING: dict[str, tuple[float, float]] = {
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022":  (0.80,  4.00),
    "claude-3-opus-20240229":     (15.00, 75.00),
}
```

#### Provider factory update in `cli.py`

```python
def _load_provider() -> object:
    provider_name = os.getenv("MODEL_PROVIDER", "gemini")
    if provider_name == "gemini":
        from monkeybot.providers.gemini import GeminiProvider
        return GeminiProvider()
    if provider_name == "claude":
        from monkeybot.providers.claude import ClaudeProvider
        return ClaudeProvider()
    raise ValueError(
        f"Unknown MODEL_PROVIDER: {provider_name!r}. Supported: gemini, claude"
    )
```

---

### 3. `core/safety.py` — Inspector Factory

#### `load_inspectors(config: dict[str, Any]) -> list[ToolInspector]`

```python
def load_inspectors(config: dict[str, Any]) -> list[ToolInspector]: ...
```

**Input — full parsed `config.yaml` dict (or `{}`):**

```yaml
safety:
  command_tiers:
    pre_approved:      [read_file, list_skills, search_memory]
    requires_approval: [write_file]
    denied:            [run_command]
  denied_patterns:
    - "rm -rf"
    - "/etc/passwd"
    - "DROP TABLE"
```

**Behaviour:**

| Condition | Result |
|-----------|--------|
| `config` is `{}` or no `safety` key | `[]` — dev mode, allow all |
| `safety.command_tiers` present | Prepend `CommandTierInspector(tiers)` |
| `safety.denied_patterns` present | Append `RulesInspector(patterns)` |
| Both present | `[CommandTierInspector, RulesInspector]` — tier check first |

**Raises:** Nothing. Missing/malformed YAML keys treated as absent.

**`cli.py` update:**

```python
# Before (E1)
from monkeybot.core.inspector import RulesInspector
def _load_inspectors(bot_config): ...  # 8 lines

# After (E2)
from monkeybot.core.safety import load_inspectors
def _load_inspectors(bot_config: dict[str, Any]) -> list[object]:
    return load_inspectors(bot_config)
```

---

### 4. `gateway/webhook.py` — Generic WebhookGateway

This is the only gateway the framework ships. It knows nothing about any chat platform.

#### `MessageExtractor` type

```python
# Type alias — the user implements this in {bot_dir}/webhook.py
MessageExtractor = Callable[[dict[str, Any]], str | None]
ResponseFormatter = Callable[[str], dict[str, Any]]
```

#### `WebhookGateway` class

```python
class WebhookGateway:
    def __init__(
        self,
        loop: AgentLoop,
        session_id_fn: Callable[[dict[str, Any]], str],
        extract_message: MessageExtractor,
        format_response: ResponseFormatter | None = None,
    ) -> None: ...

    def build_app(self) -> FastAPI: ...
```

**`format_response` default:** `lambda text: {"text": text}` — compatible with most chat platforms out of the box.

**`session_id_fn`:** User-provided callable that derives a stable session ID from the raw payload. Enables per-user-per-space conversation continuity for any platform.

#### `POST /webhook`

**Request:** Any valid JSON body (platform-specific, opaque to framework).

**Response (200):** Return value of `format_response(agent_text)`.

**Behaviour:**
1. Parse raw JSON body → `payload: dict`
2. If `WEBHOOK_SECRET` is set → verify `Authorization: Bearer {token}` using HMAC-SHA256; return `401` on failure
3. `user_message = extract_message(payload)` — if `None`, return `format_response("")` immediately (no-op)
4. `session_id = session_id_fn(payload)`
5. Collect `AssistantDelta` events from `loop.run(user_message, session_id)`
6. Return `format_response(full_response)`

#### `GET /health`

```json
{"status": "ok"}
```

No authentication. Used by load balancers, Docker HEALTHCHECK, any uptime monitor.

#### `monkeybot serve` — how extractors are loaded

`monkeybot serve` loads `{bot_dir}/webhook.py` dynamically at startup:

```python
# gateway/webhook.py — loader
def load_bot_webhook(bot_dir: str) -> tuple[MessageExtractor, ResponseFormatter, SessionIdFn]:
    webhook_path = Path(bot_dir) / "webhook.py"
    if webhook_path.exists():
        spec = importlib.util.spec_from_file_location("bot_webhook", webhook_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        extract = getattr(mod, "extract_message")
        fmt = getattr(mod, "format_response", lambda text: {"text": text})
        session_fn = getattr(mod, "session_id", lambda p: str(ulid.new()))
        return extract, fmt, session_fn
    # Fallback — generic extractor for simple webhooks and testing
    return _generic_extract, lambda text: {"text": text}, lambda p: str(ulid.new())

def _generic_extract(payload: dict[str, Any]) -> str | None:
    return (
        payload.get("text")
        or (payload.get("message") or {}).get("text")
        or None
    )
```

---

### 5. `monkeybot serve` CLI command

```
monkeybot serve --bot-dir PATH [--host HOST] [--port PORT]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-dir` | required | Bot dir with `AGENT.md`, `config.yaml`, `webhook.py` |
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8080` | Bind port |

**Startup sequence:**
1. Load bot config from `config.yaml`
2. `_load_provider()` — raises immediately if API key absent
3. `_load_inspectors(bot_config)` → `load_inspectors()`
4. Init `ConversationHistory` — creates SQLite DB if missing
5. Build `AgentLoop`
6. `load_bot_webhook(bot_dir)` → `extract_message`, `format_response`, `session_id_fn`
7. Build `WebhookGateway(loop, session_id_fn, extract_message, format_response).build_app()`
8. `uvicorn.run(app, host=host, port=port, log_config=None)`

---

### 6. Reference Extractors — `bots/example-bot/`

These are **user-land files**, not part of the installed framework package. They serve as copy-paste starting points.

#### `bots/example-bot/webhook.py` — Google Chat extractor

```python
"""Google Chat webhook extractor for MonkeyBot.

Copy this file to your bot dir and adjust as needed.
Google Chat docs: https://developers.google.com/chat/api/guides/message-formats/events
"""
from __future__ import annotations
from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    """Return message text for MESSAGE events; None for space lifecycle events."""
    if payload.get("type") != "MESSAGE":
        return None
    return (payload.get("message") or {}).get("text") or None


def format_response(text: str) -> dict[str, Any]:
    """Google Chat expects {"text": "..."}."""
    return {"text": text}


def session_id(payload: dict[str, Any]) -> str:
    """Stable per-user-per-space session ID."""
    space = (payload.get("space") or {}).get("name", "unknown-space")
    sender = (payload.get("sender") or {}).get("name", "unknown-user")
    return f"{space}/{sender}"
```

#### `bots/example-bot/webhook_slack_example.py` — Slack extractor

```python
"""Slack webhook extractor example for MonkeyBot.

Copy to webhook.py in your bot dir and adjust.
Slack Events API docs: https://api.slack.com/apis/connections/events-api
"""
from __future__ import annotations
from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    """Handle Slack event_callback MESSAGE events; ignore bot messages."""
    event = payload.get("event") or {}
    if event.get("type") != "message":
        return None
    if event.get("subtype"):  # bot_message, message_changed, etc.
        return None
    return event.get("text") or None


def format_response(text: str) -> dict[str, Any]:
    """Slack expects {"text": "..."} for simple responses."""
    return {"text": text}


def session_id(payload: dict[str, Any]) -> str:
    """Stable per-user-per-channel session ID."""
    event = payload.get("event") or {}
    channel = event.get("channel", "unknown-channel")
    user = event.get("user", "unknown-user")
    return f"{channel}/{user}"
```

---

### 7. Built-in Skills — `.agents/skills/`

4 skills ship as pure-markdown files. The `list_skills()` tool from E1 already scans this directory. No Python required.

| Skill | Instructs agent to... |
|-------|-----------------------|
| `memory-save` | Call `write_file(path="{memory_path}/{topic}.md", content=...)` to persist information |
| `memory-search` | Call `search_memory(query=..., top_k=5)` and summarise results |
| `file-ops` | Use `read_file()` / `write_file()` for bot-dir file operations |
| `self-improve` | Read its own `AGENT.md`, reflect on a lesson, append it as a new section via `write_file()` |

**Web/search skill:** Intentionally omitted. Users bring their own search API and drop a `SKILL.md` that tells the agent how to call it. The self-improve skill demonstrates the pattern of calling an external tool via `run_command`.

---

### 8. Docker Build Contract

#### `docker/Dockerfile` — framework base image only

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir uv && \
    uv pip install --system ".[gemini,claude,gchat]"
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
# No CMD — users set this in their own Dockerfile or compose file
```

**No bot dir is baked in.** Users extend the image:

```dockerfile
# User's Dockerfile
FROM monkeybot:2.0.0
COPY ./my-bot/ /bot/
CMD ["monkeybot", "serve", "--bot-dir", "/bot"]
```

Or mount at runtime:
```bash
docker run -v ./my-bot:/bot -e GEMINI_API_KEY=... monkeybot:2.0.0 \
  monkeybot serve --bot-dir /bot
```

#### `docker/docker-compose.yml` — local dev with volume mount

```yaml
services:
  monkeybot:
    build: ..
    ports:
      - "8080:8080"
    volumes:
      - ../bots/example-bot:/bot   # mount bot dir — no bake
      - ./data:/app/data            # persist SQLite
    environment:
      - GEMINI_API_KEY=${GEMINI_API_KEY:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - MODEL_PROVIDER=${MODEL_PROVIDER:-gemini}
      - WEBHOOK_SECRET=${WEBHOOK_SECRET:-}
    command: ["monkeybot", "serve", "--bot-dir", "/bot"]
```

**Deploy scripts are not framework artefacts.** Users write their own for whatever host they use. The example bot's `README.md` links to deploy guides for common platforms (AWS ECS, GCP Cloud Run, Fly.io, Render).

---

## Integration Points

### External Services

| Service | Called By | Purpose | Auth | Failure Handling |
|---------|-----------|---------|------|-----------------|
| Anthropic API | `ClaudeProvider.stream()` | LLM inference | `ANTHROPIC_API_KEY` header | Log + `ErrorEvent` in loop |
| Google Gemini API | `GeminiProvider.stream()` (E1) | LLM inference | `GEMINI_API_KEY` | Log + `ErrorEvent` in loop |
| Any chat platform | Inbound webhooks | User messages | Platform-specific (user's `webhook.py`) | `401` or `400` at gateway |
| Any container host | Outbound deploys | Hosting | User-managed credentials | Framework has no knowledge |

### No Events Published

E2 is synchronous HTTP only. No pub/sub, no event bus.

### Dependency Analysis

**E2 depends on (from E1):**

| Component | Criticality | Used By |
|-----------|-------------|---------|
| `AgentLoop` | Critical | `WebhookGateway`, `monkeybot serve` |
| `ConversationHistory` | High | All gateways — same SQLite DB |
| `CommandTierInspector`, `RulesInspector` | High | `load_inspectors()` |
| `list_skills()`, `read_file()` | Medium | Skills discovery |

**Nothing depends on E2's internals yet** — E3 adds scheduling that calls `AgentLoop`.

---

## Testing Strategy

### Unit Tests

**Coverage target:** 90% on all new `src/` modules.

**`tests/unit/test_safety.py`**

| Test | Verifies |
|------|---------|
| `test_empty_config_returns_no_inspectors` | `load_inspectors({})` → `[]` |
| `test_denied_tool_blocked` | `CommandTierInspector` → `Decision(kind="deny")` for denied tool |
| `test_pre_approved_tool_allowed` | → `Decision(kind="allow")` |
| `test_requires_approval_tool` | → `Decision(kind="approve")` |
| `test_rules_inspector_blocks_pattern` | `RulesInspector` blocks `"rm -rf"` in args |
| `test_rules_inspector_allows_clean_args` | → allow for clean args |
| `test_load_inspectors_both_present` | Returns `[CommandTierInspector, RulesInspector]` |
| `test_load_inspectors_missing_safety_key` | Returns `[]` |
| `test_tier_check_runs_before_rules` | Denied tool blocked at tier before rules |

**`tests/unit/test_claude_provider.py`** (mocked `anthropic.AsyncClient`)

| Test | Verifies |
|------|---------|
| `test_missing_api_key_raises_on_init` | `ValueError` at `ClaudeProvider()` when key absent |
| `test_stream_yields_text_delta` | Mocked stream → `TextDelta` events |
| `test_stream_yields_tool_call` | Mocked stream → `ToolCall` with parsed args |
| `test_stream_always_yields_provider_done` | `ProviderDone` is last event |
| `test_message_conversion_tool_result` | `role="tool"` → Anthropic `tool_result` block |
| `test_cost_estimation` | Correct USD via `estimate_cost()` |

**`tests/unit/test_utils.py`**

| Test | Verifies |
|------|---------|
| `test_estimate_cost_known_model` | Correct USD for `claude-3-5-sonnet` |
| `test_estimate_cost_unknown_model` | Returns `0.0` for unknown model |

### Integration Tests

**`tests/integration/test_gateway.py`** — FastAPI `TestClient`, `AgentLoop` mocked

| Test | Verifies |
|------|---------|
| `test_health_returns_ok` | `GET /health` → `{"status": "ok"}` — no auth required |
| `test_webhook_returns_agent_response` | `POST /webhook` with valid payload → agent text in response |
| `test_webhook_extract_returns_none` | Extractor returns `None` → empty response, no agent call |
| `test_webhook_invalid_json_returns_422` | Non-JSON body → `422` |
| `test_webhook_secret_rejects_bad_token` | `WEBHOOK_SECRET` set + wrong token → `401` |
| `test_webhook_secret_allows_good_token` | Correct HMAC token → `200` |
| `test_google_chat_extractor_message_event` | Reference extractor returns text for `MESSAGE` type |
| `test_google_chat_extractor_space_event` | Reference extractor returns `None` for `ADDED_TO_SPACE` |
| `test_slack_extractor_message_event` | Slack reference extractor returns text |
| `test_slack_extractor_bot_message_ignored` | Subtype `bot_message` → returns `None` |

**`tests/integration/test_claude_provider.py`** (live — gated)

```python
@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
async def test_claude_stream_live(): ...
```

### End-to-End

**`tests/test_e2_cold_start.py`** — `GEMINI_API_KEY` required; starts `monkeybot serve`, hits `/health`.

---

## Next Steps

- **Phase 1C:** Security (WEBHOOK_SECRET HMAC verification depth, secret rotation), performance (uvicorn worker tuning, async `load_bot_webhook`), observability (structured webhook request logs, token usage per request), Docker image size optimisation
