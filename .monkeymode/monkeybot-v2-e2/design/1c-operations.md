# Design: monkeybot-v2-e2 — Safety, Skills & Production Gateway
## Phase 1C: Production Readiness

**Date:** 2026-05-13  
**Status:** Phase 1C — Security, Performance, Deployment, Observability, Risk

> **Baseline:** E1's `1c-operations.md` covers the core threat model, `run_command` injection defense, `read_file`/`write_file` path constraints, SQLite WAL mode, import budget, and cloud-agnostic deployment. This document covers only what E2 adds or changes. Read E1's 1C alongside this.

---

## Security Design

### New Attack Surface in E2

E2 opens an HTTP endpoint to the public internet. This is a fundamentally larger attack surface than E1's local CLI.

```
Threat surface additions in E2:
CRITICAL  /webhook — unauthenticated by default; publicly reachable
HIGH      load_bot_webhook() — dynamic import of user code from bot dir
HIGH      ANTHROPIC_API_KEY — new secret, same protection rules as GEMINI_API_KEY
MEDIUM    WebhookGateway payload — arbitrary JSON from any caller
LOW       ClaudeProvider streaming — same isolation as GeminiProvider
```

### Webhook Authentication — `WEBHOOK_SECRET`

The framework ships an opt-in HMAC-SHA256 verifier. Platforms that sign webhook payloads (Slack, GitHub, most REST webhooks) use this pattern natively.

**Verification flow:**
```
Inbound request
    │
    ├── WEBHOOK_SECRET not set → skip (dev mode, log WARNING once at startup)
    │
    └── WEBHOOK_SECRET set
            │
            ├── Check Authorization header or X-Hub-Signature-256 header
            │
            ├── HMAC-SHA256(WEBHOOK_SECRET, raw_body) == signature? → proceed
            │
            └── mismatch → HTTP 401, log WARNING with source IP (not payload)
```

**Implementation in `gateway/webhook.py`:**
```python
import hashlib, hmac

def _verify_hmac(secret: str, raw_body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header)
```

`hmac.compare_digest` prevents timing attacks. Raw body bytes are used (before JSON parsing) — same as Slack/GitHub signing.

**Google Chat uses Google-signed JWT tokens**, not HMAC. A user who needs this sets `WEBHOOK_SECRET=""` and adds JWT verification in their `webhook.py`'s `extract_message()` before returning the text. The framework does not need to know the platform.

### Webhook Payload Limits

```python
# gateway/webhook.py — FastAPI body size limit
from fastapi import Request

MAX_PAYLOAD_BYTES = 64 * 1024  # 64KB — sufficient for any chat message

@app.post("/webhook")
async def webhook(request: Request) -> dict:
    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    payload = json.loads(body)
    ...
```

64KB is generous for a chat message. Prevents memory exhaustion from oversized payloads.

### Dynamic `webhook.py` Import Safety

`load_bot_webhook()` does `exec_module()` on user-supplied code. This is intentional — the user owns the bot dir. Security properties:
- The module runs in the same process and Python environment — no sandboxing
- This is no different from the user writing any other Python file that gets imported
- **The operator is responsible for the contents of their bot dir** — this is documented clearly
- **Never load `webhook.py` from an untrusted path** — the bot dir should be under operator control (local filesystem or operator-built container image)

Framework defence: if `webhook.py` raises any exception during import or during `extract_message()`, the gateway returns HTTP 500 and logs the error without exposing the traceback to the caller.

### `ANTHROPIC_API_KEY` Protection

Same rules as `GEMINI_API_KEY` from E1:
- Accessed only inside `ClaudeProvider.__init__()` and `stream()` — never in module scope
- Redacted from subprocess environment in `run_command`'s `_safe_env()` (E1 already covers this via `REDACTED_VARS` set)
- Never logged

Add to `REDACTED_VARS` in E1's `run_command.py`:
```python
REDACTED_VARS = {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "DB_URL", "WEBHOOK_SECRET"}
```

`WEBHOOK_SECRET` added — if a user passes it as an env var, the subprocess should not be able to read it.

### Webhook Payload — What Not to Log

```
NEVER log:
- Raw webhook payload body (may contain user PII, tokens, message content)
- Authorization header value
- WEBHOOK_SECRET value
- Full user message text (same policy as E1: log length, not content)

SAFE to log:
- HTTP method, path, status, duration_ms
- Content-Length header
- Source IP (for rate limit debugging)
- session_id (derived identifier, not raw payload field)
```

### Secrets Management Summary (E2 additions)

| Secret | Storage | Loaded By | Never Do |
|--------|---------|-----------|----------|
| `ANTHROPIC_API_KEY` | Env var | `ClaudeProvider.__init__()` | Log, pass to subprocess, default value in code |
| `WEBHOOK_SECRET` | Env var | `gateway/webhook.py` at startup | Log, expose in error responses |

---

## Performance & Scalability

### Import Budget (E2 additions)

E1 enforces < 200ms for `import monkeybot`. E2 adds `fastapi` and `uvicorn` to the installed package but **must not import them at module top level**. They are only imported inside `monkeybot serve`.

| Import | Cost | When |
|--------|------|------|
| `fastapi` | ~40ms | Inside `serve` command only |
| `uvicorn` | ~30ms | Inside `serve` command only |
| `anthropic` | ~80ms | Inside `ClaudeProvider.stream()` only — lazy |

The `import monkeybot` cold-start budget is unchanged at < 200ms. `monkeybot serve` startup time budget: < 2s (dominated by uvicorn bind + DB init + provider validation).

### Webhook Response Latency

The webhook endpoint has a different latency profile to the CLI — callers (chat platforms) typically have a timeout of 30s before they show an error to the user.

| Component | Budget | Notes |
|-----------|--------|-------|
| JSON parse + `extract_message()` | < 5ms | Sync, trivial |
| `session_id()` | < 1ms | String concat |
| `history.load()` | < 50ms | SQLite indexed read |
| LLM first token (Gemini) | ~800ms | Network RTT to API |
| LLM first token (Claude) | ~600ms | Network RTT to API |
| Tool calls (if any) | variable | |
| `history.save()` | < 10ms | SQLite write, WAL mode |
| `format_response()` + JSON serialize | < 1ms | |
| **Total (no tool calls)** | **~1–2s** | Well within 30s platform timeout |

> **Streaming note:** The webhook endpoint collects the full response before returning — chat platforms expect a complete reply in the response body, not a stream. This is correct behaviour.

### Concurrent Request Handling

`uvicorn` + `FastAPI` are async. Multiple webhook requests are handled concurrently within a single uvicorn worker. The `AgentLoop.run()` is an `async` generator — each request gets its own event loop task.

**SQLite concurrency:** WAL mode (E1) allows concurrent readers and one writer. Multiple simultaneous webhook requests hitting `history.load()` (reads) are safe. `history.save()` (write) serialises naturally via SQLite's WAL lock. No additional concurrency control needed at this scale.

**Single worker is correct for E2:** Multiple uvicorn workers would each open their own SQLite connection to the same file. This works with WAL mode, but adds complexity. Start with `workers=1`; horizontal scale is achieved by multiple containers sharing a volume mount.

```python
# cli.py — monkeybot serve
uvicorn.run(app, host=host, port=port, workers=1, log_config=None)
```

### Scalability Path

| Dimension | E2 Behaviour | Scale Path |
|-----------|-------------|-----------|
| Concurrent webhooks | Async, single worker | Multiple containers + shared SQLite volume (WAL) |
| Sessions | 1 session per user/space | Unbounded — SQLite handles thousands |
| Message history per session | Unbounded in E2 | Context window summarisation in E3 |
| Provider rate limits | No retry in E2 | Retry with backoff in E3 |

---

## Deployment Strategy

### Multi-Stage Dockerfile (upgrade from E1 draft)

E1's 1C proposed a multi-stage build. E2 ships it. Key changes from E1's sketch:
- No bot dir baked in (`COPY bots/` removed)
- `EXTRAS` build arg selects provider(s)
- Non-root user enforced
- Python `urllib` healthcheck (no `curl` dependency in slim image)

```dockerfile
# docker/Dockerfile

# Stage 1: install deps
FROM python:3.11-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml .
COPY src/ src/
ARG EXTRAS="gemini,gchat"
RUN uv pip install --system ".[${EXTRAS}]"

# Stage 2: runtime
FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin/monkeybot /usr/local/bin/monkeybot
COPY src/ src/

# Non-root user — security best practice
RUN adduser --disabled-password --gecos "" monkeybot
USER monkeybot

# Data dirs — user mounts a volume here
RUN mkdir -p /data/memory

ENV PYTHONUNBUFFERED=1
ENV DB_URL="sqlite:////data/monkeybot.db"
ENV MEMORY_PATH="/data/memory"

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# No CMD — users set this; default shown in docker-compose.yml
```

**Image size targets:**

| Extras installed | Target size |
|-----------------|-------------|
| `gemini,gchat` (default) | < 250MB |
| `claude,gchat` | < 280MB |
| `gemini,claude,gchat` | < 320MB |

**Build examples:**
```bash
# Default (Gemini + webhook server)
docker build -t monkeybot:2.0.0 .

# Claude-only
docker build --build-arg EXTRAS="claude,gchat" -t monkeybot:2.0.0-claude .

# Both providers
docker build --build-arg EXTRAS="gemini,claude,gchat" -t monkeybot:2.0.0-full .
```

### `docker-compose.yml` — Local Dev with Volume Mount

```yaml
# docker/docker-compose.yml
services:
  monkeybot:
    build:
      context: ..
      args:
        EXTRAS: "gemini,gchat"
    ports:
      - "8080:8080"
    volumes:
      - ../bots/example-bot:/bot:ro   # bot dir read-only mount
      - monkeybot-data:/data           # persistent SQLite + memory
    env_file:
      - ../.env
    environment:
      - SKILLS_PATH=/bot/.agents/skills
    command: ["monkeybot", "serve", "--bot-dir", "/bot"]

volumes:
  monkeybot-data:
```

**`/bot` is read-only** — the container cannot modify the bot configuration at runtime.

### Extending the Base Image (user pattern)

```dockerfile
# User's Dockerfile — in their bot repo
FROM monkeybot:2.0.0
COPY ./my-bot/ /bot/
CMD ["monkeybot", "serve", "--bot-dir", "/bot"]
```

```bash
# Deploy anywhere
docker build -t my-bot:latest .

# AWS ECS / Fargate
aws ecs update-service --cluster prod --service my-bot --force-new-deployment

# Fly.io
fly deploy

# Self-hosted
docker run -d -p 8080:8080 \
  -e GEMINI_API_KEY=$GEMINI_API_KEY \
  -v /data/my-bot:/data \
  my-bot:latest
```

The framework ships zero platform-specific deploy scripts. The example bot's README links to community guides for each platform.

### CI Pipeline Updates

```
git push
    │
    ▼
ruff + mypy + pytest (unit) — existing E1 gates
    │ pass
    ▼
pytest tests/integration/test_gateway.py  ← NEW in E2
    │ pass
    ▼
docker build (EXTRAS=gemini,gchat) — verify image builds
    │ pass
    ▼
docker run GET /health → {"status":"ok"}  ← NEW in E2
    │ pass
    ▼
[on tag] docker push → registry
```

### Rollback Plan (additions to E1)

| Trigger | Action | Time Target |
|---------|--------|-------------|
| `/health` returns non-200 after deploy | Roll back container to previous image tag | < 2 min |
| Webhook 5xx error spike | Same — container rollback | < 2 min |
| `ClaudeProvider` fails (bad API key) | Set `MODEL_PROVIDER=gemini` env var + restart | < 1 min |
| Bad `webhook.py` (import error) | Fix file in bot dir + restart container | < 1 min |

---

## Observability

### Structured Logging — Webhook Request Fields (additions to E1)

E1 established the JSON log format. E2 adds webhook-specific fields:

```json
{
  "timestamp": "2026-05-13T20:00:01.234Z",
  "level": "INFO",
  "service": "monkeybot",
  "event": "webhook_request",
  "session_id": "spaces/ABC/users/XYZ",
  "duration_ms": 1823,
  "input_tokens": 512,
  "output_tokens": 128,
  "cost_usd": 0.0004,
  "status": 200
}
```

**What to log at webhook entry (INFO):**
- `event: "webhook_request"`, `status`, `duration_ms`
- `session_id` (derived, not raw payload)
- Token counts + cost (from `TurnComplete`)

**What to log at webhook entry (WARNING):**
- `event: "webhook_auth_failed"`, source IP — when `WEBHOOK_SECRET` set and verification fails
- `event: "webhook_extract_none"` — when extractor returns `None` (space event, bot message, etc.)
- `event: "webhook_secret_not_set"` — logged ONCE at startup, not per request

**What NOT to log:**
- Raw webhook payload (may contain user messages, tokens, PII)
- `Authorization` header value
- Full user message text (log `message_length_chars` instead)

### New Startup Log Fields

```json
{
  "event": "serve_startup",
  "provider": "gemini",
  "bot_dir": "/bot",
  "webhook_auth": "hmac-sha256",
  "skills_count": 4,
  "message": "MonkeyBot serve ready"
}
```

`webhook_auth` is `"none"` when `WEBHOOK_SECRET` is not set — visible in logs so operators notice they're running unprotected.

### Health Check Semantics

`GET /health` verifies the process is alive but does NOT probe the LLM API or SQLite on every call — that would add latency and quota consumption to every load balancer ping.

**Liveness only:** Returns `{"status": "ok"}` as long as uvicorn is running.

A deeper readiness check (DB reachable, provider credentials valid) could be added at `GET /ready` in a later epic if needed.

---

## Risk Assessment

*Additions to E1's risk table:*

| Risk | Likelihood | Impact | Mitigation | Epic |
|------|-----------|--------|------------|------|
| `/webhook` flooded with unauthenticated requests | High | Medium | Set `WEBHOOK_SECRET`; platforms (Slack, Google Chat) sign all requests. Platform's own infra is the first line of defence. Framework documents this clearly. | E2 — accept, document |
| `extract_message()` raises unhandled exception | Medium | Low | Gateway wraps in `try/except`, returns 500, logs error without payload | E2 |
| `webhook.py` has import error (syntax/missing dep) | Low | High | `load_bot_webhook()` catches `ImportError`, raises with clear message pointing to `webhook.py` path | E2 |
| Multiple containers writing to same SQLite file | Medium | Medium | WAL mode (E1) handles concurrent writers safely at low scale; for higher scale, use shared volume + single writer instance | E2 — accept, document |
| Claude API rate limit exceeded | Low | Medium | `ProviderDone` with `ErrorEvent`; no retry in E2; log error. User sees agent's error message in chat response | E2 — accept, retry in E3 |
| `ANTHROPIC_API_KEY` or `WEBHOOK_SECRET` in container ENV dumped via `run_command` | Low | High | `REDACTED_VARS` in `run_command._safe_env()` includes both new secrets | E2 |
| User's `format_response()` returns non-JSON-serialisable dict | Low | Low | FastAPI's JSON serialiser raises `ValueError`; caught at gateway level, returns 500 with clear log | E2 |
| Chat platform retries failed webhooks, causing duplicate agent turns | Medium | Low | Idempotency via `session_id` in history: duplicate user message stored but agent sees it as a new message. Acceptable in E2; idempotency key deduplication is E3 | E2 — accept |
| Docker image with all provider SDKs is too large | Low | Low | `EXTRAS` build arg selects only needed provider; documented in Dockerfile | E2 |

---

## Final Sign-Off Checklist

- [x] Security: HTTP endpoint protected by opt-in HMAC-SHA256 (`WEBHOOK_SECRET`)
- [x] Security: Webhook payload size limited to 64KB
- [x] Security: `WEBHOOK_SECRET` and `ANTHROPIC_API_KEY` added to `REDACTED_VARS`
- [x] Security: Dynamic `webhook.py` import documented as operator-responsibility; exceptions caught
- [x] Security: Payload content never logged — only derived session_id and metadata
- [x] Security: Non-root user in Docker image
- [x] Performance: `fastapi`/`uvicorn`/`anthropic` not imported at module top level — cold-start budget preserved
- [x] Performance: Single uvicorn worker; WAL mode handles concurrent webhook reads
- [x] Performance: Webhook response < 30s platform timeout; typical < 2s (LLM latency dominates)
- [x] Deployment: Multi-stage Dockerfile; no bot dir baked in; `EXTRAS` build arg
- [x] Deployment: `docker-compose.yml` mounts bot dir as read-only volume
- [x] Deployment: No platform-specific deploy scripts in framework; example bot README links to guides
- [x] Observability: Webhook request log includes `session_id`, `duration_ms`, `status`, token counts
- [x] Observability: Startup log includes `webhook_auth: "none"` warning when secret not set
- [x] Observability: Health check is liveness-only; no LLM probe on every ping
- [x] Risk: Webhook flood documented; mitigation = `WEBHOOK_SECRET` + platform's signing
- [x] Risk: All new secrets in `REDACTED_VARS`; no secret ever logged
- [x] Risk: Duplicate webhook retries accepted in E2; idempotency deferred to E3
