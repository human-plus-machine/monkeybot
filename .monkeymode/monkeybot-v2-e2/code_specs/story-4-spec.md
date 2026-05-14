# Code Spec: Story 4 — WebhookGateway, serve CLI & Docker

**Story:** User Story 4 — WebhookGateway, serve CLI & Docker  
**Design Reference:** 1A ADR-E2-004, ADR-E2-005, 1B `gateway/webhook.py`, 1B `monkeybot serve`, 1C Docker  
**Date:** 2026-05-13  

## Implementation Summary

- **Files to Create:** 7 files
- **Files to Modify:** 3 files (`cli.py`, `bots/example-bot/config.yaml`, `src/monkeybot/__init__.py`)
- **Estimated Complexity:** L

## Codebase Conventions

Same project-wide conventions. Pattern references:
- `src/monkeybot/gateway/cli.py` → `CLIGateway` structure to mirror for `WebhookGateway`
- `src/monkeybot/cli.py` `run` command → mirror for `serve` command
- `src/monkeybot/__init__.py` → lazy `__getattr__` pattern for new exports

## Technical Context

**Key Gotchas:**
- `fastapi` and `uvicorn` must be imported **lazily** inside the `serve` command and `build_app()` — same cold-start rule as provider SDKs.
- `build_app()` returns a **new** `FastAPI()` instance on each call — do not store as instance variable.
- `load_bot_webhook()` uses `importlib.util.spec_from_file_location` + `exec_module` to load the user's `webhook.py`. Wrap in `try/except Exception` and raise `ImportError(f"Failed to load {path}: {exc}")`.
- Story 2's `core/safety.py` must exist before this story runs. `cli.py` changes import `load_inspectors` from `core/safety`.
- HMAC verification: accept both `sha256=<hex>` and bare `<hex>` header formats.
- If `WEBHOOK_SECRET` not set, log a single WARNING at server startup (not per-request).
- `POST /webhook` body size limit: 64KB (`413` if exceeded).
- `AgentLoop.run()` yields `AgentEvent` objects — only collect `AssistantDelta` for the response text.

**Reusable Utilities (from E1):**
- `monkeybot.core.loop`: `AgentLoop`
- `monkeybot.core.events`: `AssistantDelta`
- `monkeybot.core.safety`: `load_inspectors` (Story 2)

## Task Breakdown

### Task 1: Create `gateway/webhook.py`

**Dependencies:** Story 2 (`core/safety.py`)  
**Files:** `src/monkeybot/gateway/webhook.py` (create)

**Type aliases:**
```python
from collections.abc import Callable
from typing import Any

MessageExtractor = Callable[[dict[str, Any]], str | None]
ResponseFormatter = Callable[[str], dict[str, Any]]
SessionIdFn = Callable[[dict[str, Any]], str]
```

**`WebhookGateway` class:**
```python
class WebhookGateway:
    def __init__(
        self,
        loop: AgentLoop,
        session_id_fn: SessionIdFn,
        extract_message: MessageExtractor,
        format_response: ResponseFormatter | None = None,
    ) -> None:
        self._loop = loop
        self._session_id_fn = session_id_fn
        self._extract = extract_message
        self._format = format_response or (lambda text: {"text": text})

    def build_app(self) -> "FastAPI":  # lazy import — FastAPI quoted to avoid module-level import
        ...
```

**`POST /webhook` handler** — full flow specified in user_stories.md "Implementation Details". Key points:
1. Read raw body
2. Check size > 64KB → 413
3. If `WEBHOOK_SECRET` set, verify HMAC → 401 if invalid
4. Parse JSON
5. `extract_message(payload)` → if None, return `format("")` immediately
6. `session_id_fn(payload)` → get session ID
7. Collect `AssistantDelta.text` from `loop.run()` → join → `format(response)`

**`GET /health`:** `return {"status": "ok"}` — no auth.

**`_verify_hmac` private function** (module-level):
```python
def _verify_hmac(secret: str, body: bytes, header: str | None) -> bool:
    import hashlib, hmac as _hmac
    if not header:
        return False
    expected_hex = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    expected_full = "sha256=" + expected_hex
    return _hmac.compare_digest(expected_full, header) or _hmac.compare_digest(expected_hex, header)
```

**`load_bot_webhook` function:**
```python
def load_bot_webhook(bot_dir: str) -> tuple[MessageExtractor, ResponseFormatter, SessionIdFn]:
    """Load extract_message, format_response, session_id from {bot_dir}/webhook.py.
    Falls back to generic extractor if file absent.
    """
    webhook_path = Path(bot_dir) / "webhook.py"
    if not webhook_path.exists():
        return _generic_extract, _generic_format, _generic_session_id
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bot_webhook", webhook_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module.extract_message, module.format_response, module.session_id
    except Exception as exc:
        raise ImportError(f"Failed to load {webhook_path}: {exc}") from exc
```

**Generic fallback functions:**
```python
def _generic_extract(payload: dict[str, Any]) -> str | None:
    return payload.get("text") or payload.get("message") or payload.get("content")

def _generic_format(text: str) -> dict[str, Any]:
    return {"text": text}

def _generic_session_id(payload: dict[str, Any]) -> str:
    return payload.get("session_id") or payload.get("user") or str(ulid.new())
```

---

### Task 2: Create `bots/example-bot/webhook.py` (Google Chat extractor)

**Dependencies:** Task 1  
**Files:** `bots/example-bot/webhook.py` (create)

```python
"""Google Chat webhook extractor for MonkeyBot."""
from __future__ import annotations
from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    """Extract text from Google Chat MESSAGE event. Returns None for non-message events."""
    if payload.get("type") == "ADDED_TO_SPACE":
        return None
    return (payload.get("message") or {}).get("text")


def format_response(text: str) -> dict[str, Any]:
    """Format response as Google Chat card."""
    return {"text": text}


def session_id(payload: dict[str, Any]) -> str:
    """Use space name as session ID for conversation continuity."""
    return (payload.get("space") or {}).get("name") or "default"
```

---

### Task 3: Create `bots/example-bot/webhook_slack_example.py` (Slack reference)

**Dependencies:** Task 1  
**Files:** `bots/example-bot/webhook_slack_example.py` (create)

```python
"""Slack webhook extractor reference for MonkeyBot."""
from __future__ import annotations
from typing import Any


def extract_message(payload: dict[str, Any]) -> str | None:
    """Extract text from Slack Events API payload. Returns None for bot messages."""
    event = payload.get("event") or {}
    if event.get("subtype") == "bot_message":
        return None
    return event.get("text")


def format_response(text: str) -> dict[str, Any]:
    return {"text": text}


def session_id(payload: dict[str, Any]) -> str:
    event = payload.get("event") or {}
    return event.get("channel") or "default"
```

---

### Task 4: Update `cli.py`

**Dependencies:** Story 2 (`core/safety.py`), Task 1  
**Files:** `src/monkeybot/cli.py` (modify)

**Change 1 — update `_load_inspectors`** (replace existing function body):
```python
def _load_inspectors(bot_config: dict[str, object]) -> list[object]:
    from monkeybot.core.safety import load_inspectors
    return load_inspectors(bot_config)  # type: ignore[return-value]
```

**Change 2 — update `_load_provider`** (add claude branch):
```python
def _load_provider() -> object:
    provider_name = os.getenv("MODEL_PROVIDER", "gemini")
    if provider_name == "gemini":
        from monkeybot.providers.gemini import GeminiProvider
        return GeminiProvider()
    if provider_name == "claude":
        from monkeybot.providers.claude import ClaudeProvider
        return ClaudeProvider()
    raise ValueError(f"Unknown MODEL_PROVIDER: {provider_name}. Supported: gemini, claude")
```

**Change 3 — add `serve` command and `_serve_async`** (after existing `run` command):
```python
@main.command()
@click.option("--bot-dir", required=True, type=click.Path(exists=True))
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8080, type=int)
def serve(bot_dir: str, host: str, port: int) -> None:
    """Start the webhook gateway server."""
    _setup_logging()
    asyncio.run(_serve_async(bot_dir, host, port))


async def _serve_async(bot_dir: str, host: str, port: int) -> None:
    import uvicorn  # noqa: PLC0415
    from monkeybot.gateway.webhook import WebhookGateway, load_bot_webhook

    bot_path = Path(bot_dir)
    config_path = bot_path / "config.yaml"
    bot_config: dict[str, object] = {}
    if config_path.exists():
        import yaml  # type: ignore[import-untyped]
        bot_config = yaml.safe_load(config_path.read_text()) or {}

    model_cfg = bot_config.get("model")
    default_model = (
        model_cfg.get("default", "gemini-2.0-flash")
        if isinstance(model_cfg, dict)
        else "gemini-2.0-flash"
    )

    config: dict[str, object] = {
        "agent_md_path": str(bot_path / "AGENT.md"),
        "memory_path": os.getenv("MEMORY_PATH", "./data/memory"),
        "skills_path": os.getenv("SKILLS_PATH", "./.agents/skills"),
        "bot_dir": str(bot_path),
        "model": default_model,
    }

    db_url = os.getenv("DB_URL", "sqlite:///data/monkeybot.db")
    history = ConversationHistory(db_url=db_url)
    await history.init()

    provider = _load_provider()
    inspectors = _load_inspectors(bot_config)

    agent_loop = AgentLoop(
        provider=provider,  # type: ignore[arg-type]
        history=history,
        inspectors=inspectors,  # type: ignore[arg-type]
        config=config,
    )

    extract, fmt, session_fn = load_bot_webhook(bot_dir)
    gateway = WebhookGateway(
        loop=agent_loop,
        session_id_fn=session_fn,
        extract_message=extract,
        format_response=fmt,
    )
    app = gateway.build_app()

    if not os.getenv("WEBHOOK_SECRET"):
        import logging
        logging.getLogger(__name__).warning(
            "WEBHOOK_SECRET not set — webhook endpoint is unauthenticated"
        )

    server_config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(server_config)
    await server.serve()
```

---

### Task 5: Update `__init__.py` — export `WebhookGateway`

**Dependencies:** Task 1  
**Files:** `src/monkeybot/__init__.py` (modify)

Add `"WebhookGateway"` to `__all__` and add a lazy `__getattr__` branch:
```python
__all__ = ["AgentLoop", "ConversationHistory", "Provider", "TurnContext", "WebhookGateway"]

# In __getattr__:
if name == "WebhookGateway":
    from monkeybot.gateway.webhook import WebhookGateway
    return WebhookGateway
```

---

### Task 6: Update `bots/example-bot/config.yaml`

**Dependencies:** None  
**Files:** `bots/example-bot/config.yaml` (modify)

Replace existing `safety` block with the full tier config from user_stories.md:
```yaml
safety:
  command_tiers:
    pre_approved:
      - read_file
      - list_skills
      - search_memory
    requires_approval:
      - write_file
    denied:
      - run_command
  denied_patterns:
    - "rm -rf"
    - "/etc/passwd"
    - "DROP TABLE"
```

The existing `model` block is unchanged. Existing `denied_patterns` at the top level of `safety` is replaced by the new structure above.

---

### Task 7: Docker files

**Dependencies:** None (independent of Python code)  
**Files:** `docker/Dockerfile` (create), `docker/docker-compose.yml` (create)

**`docker/Dockerfile`** — multi-stage, framework base image only:
```dockerfile
# Stage 1: builder
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir ".[gchat,gemini]"

# Stage 2: runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
RUN pip install --no-cache-dir -e . --no-deps

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["monkeybot", "serve", "--bot-dir", "/bot", "--host", "0.0.0.0", "--port", "8080"]
```

**`docker/docker-compose.yml`** — local dev with volume mounts:
```yaml
version: "3.9"
services:
  monkeybot:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "8080:8080"
    volumes:
      - ./bots/example-bot:/bot:ro
      - monkeybot_data:/app/data
    environment:
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - MODEL_PROVIDER=${MODEL_PROVIDER:-gemini}
      - WEBHOOK_SECRET=${WEBHOOK_SECRET:-}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}

volumes:
  monkeybot_data:
```

---

### Task 8: Integration tests

**Dependencies:** Task 1, Task 4  
**Files:** `tests/integration/test_gateway.py` (create), `tests/test_e2_cold_start.py` (create)

**`test_gateway.py`** — use `fastapi.testclient.TestClient` (sync). Mock `AgentLoop.run()` to yield `AssistantDelta(text="hello")` then `TurnComplete(...)`:

**Test cases:**
- `GET /health` → `{"status": "ok"}`, HTTP 200, no auth
- `POST /webhook` valid JSON, `extract_message` returns string → agent runs, `format_response` result returned
- `POST /webhook` where `extract_message` returns `None` → `format("")` returned, no agent call
- Non-JSON body → HTTP 422
- `WEBHOOK_SECRET` set + wrong HMAC → HTTP 401
- `WEBHOOK_SECRET` set + correct HMAC → HTTP 200
- `load_bot_webhook` with `webhook.py` present → all 3 callables returned
- `load_bot_webhook` with no `webhook.py` → generic fallback returned (no exception)
- `load_bot_webhook` with syntax-error `webhook.py` → `ImportError` with file path in message

```python
# Pattern for test_gateway.py:
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

def make_gateway(extract=None, fmt=None):
    from monkeybot.gateway.webhook import WebhookGateway
    mock_loop = AsyncMock()
    # Configure mock_loop.run to yield events
    ...
    return WebhookGateway(loop=mock_loop, session_id_fn=lambda p: "s1",
                          extract_message=extract or (lambda p: p.get("text")),
                          format_response=fmt)
```

**`test_e2_cold_start.py`** — verify `monkeybot serve` starts and `/health` returns 200:
```python
import os, subprocess, sys, time, threading
import pytest
import httpx

@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set")
def test_serve_health_check(tmp_path) -> None:
    """Starts server in subprocess, hits /health, asserts 200."""
    # Write minimal AGENT.md + config.yaml to tmp_path
    # subprocess.Popen(["monkeybot", "serve", "--bot-dir", str(tmp_path), "--port", "9999"])
    # Poll /health until 200 or timeout 10s
    # Assert response == {"status": "ok"}
    # Terminate process
```

## Reference Code Example

**Gateway Pattern** (from `src/monkeybot/gateway/cli.py`):
```python
class CLIGateway:
    def __init__(self, loop: AgentLoop, session_id: str) -> None:
        self._loop = loop
        self._session_id = session_id

    async def run_interactive(self) -> None:
        async for event in self._loop.run(user_input, self._session_id):
            if isinstance(event, AssistantDelta):
                print(event.text, end="", flush=True)
```

## Final Verification

**WebhookGateway:**
- [ ] `GET /health` → `{"status": "ok"}`, HTTP 200
- [ ] `POST /webhook` valid payload → agent runs, response returned
- [ ] `extract_message` returns `None` → immediate `format("")`, no agent call
- [ ] Non-JSON body → HTTP 422
- [ ] Wrong HMAC → HTTP 401; correct HMAC → HTTP 200
- [ ] `WEBHOOK_SECRET` not set → WARNING logged once at startup

**`load_bot_webhook`:**
- [ ] `webhook.py` present → all 3 callables returned
- [ ] `webhook.py` absent → generic fallback (no exception)
- [ ] Syntax error in `webhook.py` → `ImportError` with file path

**Reference extractors:**
- [ ] Google Chat `MESSAGE` event → returns `message.text`
- [ ] Google Chat `ADDED_TO_SPACE` → returns `None`
- [ ] Slack `message` event → returns `event.text`
- [ ] Slack `bot_message` subtype → returns `None`

**`cli.py` changes:**
- [ ] `monkeybot serve --bot-dir ... --port 8080` starts uvicorn, `/health` returns 200
- [ ] `MODEL_PROVIDER=claude` → `ClaudeProvider()` returned
- [ ] `_load_inspectors` delegates to `core/safety.load_inspectors()`

**Docker:**
- [ ] `docker build .` exits 0 from `docker/` directory
- [ ] `docker compose config` exits 0 (valid YAML)
- [ ] Running container `/health` returns 200

**Code Quality:**
- [ ] `fastapi` and `uvicorn` imported lazily (inside functions/methods)
- [ ] `ruff check` clean on all new/modified files
- [ ] `mypy --strict` clean on all new/modified files
