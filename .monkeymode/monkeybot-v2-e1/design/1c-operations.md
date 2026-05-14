# Design: MonkeyBot v2 E1 — Core Harness & Walking Skeleton
## Phase 1C: Production Readiness

**Date:** 2026-05-13  
**Status:** Phase 1C — Security, Performance, Deployment, Observability, Risk  
**Version:** 1.0

---

## Security Design

### Threat Model

MonkeyBot's primary attack surface is `run_command`. The LLM controls the `command` argument — if prompt injection causes the model to issue a destructive shell command, the damage happens on the operator's host. Every security decision in E1 flows from this.

```
Threat surface ranking (E1):
HIGH   run_command — LLM-controlled shell execution
HIGH   write_file  — LLM can overwrite arbitrary paths if unconstrained
MEDIUM read_file   — path traversal could expose secrets (e.g. ~/.ssh, .env)
LOW    SQLite DB   — local file, no network exposure in E1
LOW    Gemini API  — API key exposure via logs
```

### Input Validation

#### `run_command` — Command Injection Defense

```python
BLOCKED_PATTERNS = [
    # Credential exfiltration
    r"\bcat\b.*\b\.env\b",
    r"\bcat\b.*\bpassword\b",
    r"curl\s+.*\|\s*bash",
    r"wget\s+.*\|\s*bash",
    # Privilege escalation
    r"\bsudo\b",
    r"\bsu\s+-\b",
    r"\bchmod\s+[0-7]*7[0-7][0-7]\b",   # setuid/world-writable
    # Destructive operations
    r"\brm\s+-rf\s+/\b",
    r"\b:\(\)\{.*\};\s*:\b",              # fork bomb
    r"\bmkfs\b",
    r"\bdd\s+if=.*of=/dev/\b",
]
```

These patterns are applied by `RulesInspector` in E1 even before the full safety config ships in E2. In E1, the inspector list is populated from `config.yaml` at startup:

```yaml
# bots/example-bot/config.yaml
safety:
  denied_patterns:
    - "sudo"
    - "rm -rf /"
    - "curl.*|.*bash"
    - "wget.*|.*bash"
  pre_approved:
    - "echo"
    - "ls"
    - "cat"
    - "python"
    - "uv"
```

**Hard constraints (not config-overridable):**
- `run_command` working directory defaults to the bot directory — never `/` or `~`.
- Maximum command length: 4096 chars — prevents argument-smuggling via long strings.
- Timeout default: 30s, maximum: 300s — no unbounded processes.
- `os.environ.copy()` is passed to the subprocess — the process inherits the environment but cannot modify the parent's environment.

#### `read_file` — Path Traversal Defense

```python
def read_file(path: str, *, allowed_roots: list[Path] | None = None) -> str:
    """
    If allowed_roots is set, resolved path must be under one of them.
    Default allowed_roots in E1: [bot_dir, memory_path, skills_path]
    Any path outside these roots returns "ERROR: Access denied: {path}"
    """
    resolved = Path(path).resolve()
    if allowed_roots:
        if not any(
            str(resolved).startswith(str(root.resolve()))
            for root in allowed_roots
        ):
            return f"ERROR: Access denied: {path}"
    ...
```

**In E1:** The loop passes `allowed_roots` from `TurnContext` — the bot directory, memory directory, and skills directory. Access to `/etc/passwd`, `~/.ssh/id_rsa`, `.env` files outside the bot directory is blocked at the tool level.

#### `write_file` — Write Root Enforcement

Same pattern as `read_file`. Default write roots: `[memory_path, bot_dir/output]`. The model cannot write to system paths or outside the bot boundary.

#### Environment Variables — Secret Protection

```python
# In run_command: sanitize env before passing to subprocess
REDACTED_VARS = {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DB_URL"}

def _safe_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in REDACTED_VARS:
        if key in env:
            env[key] = "REDACTED"
    return env
```

The subprocess inherits a redacted copy — it cannot exfiltrate API keys via `env` output.

### Secrets Management

| Secret | Storage | How Loaded | Never Do |
|--------|---------|-----------|----------|
| `GEMINI_API_KEY` | Env var | `os.environ["GEMINI_API_KEY"]` in `GeminiProvider.stream()` | Never log, never pass to subprocess |
| `DB_URL` | Env var | `os.environ.get("DB_URL", default)` in `cli.py` | Never log full DSN |
| Bot API tokens (E2+) | Env var | Config-driven | Never commit to git |

`.env` is in `.gitignore`. `.env.example` contains only placeholder values. No secret is ever a default value in code.

### Data Classification (E1)

| Data | Classification | At Rest | In Transit |
|------|---------------|---------|-----------|
| Conversation history | Potentially sensitive | SQLite file (operator controls) | N/A (local) |
| Memory files | Potentially sensitive | Filesystem (operator controls) | N/A (local) |
| AGENT.md | Config (not secret) | Git-safe | N/A |
| API keys | Secret | Env only | TLS (to Gemini) |

Encryption at rest is the operator's responsibility (encrypted volume, OS-level FDE). MonkeyBot does not manage disk encryption — this is appropriate for a Docker-based tool.

---

## Performance & Scalability

### Performance Targets (E1)

| Metric | Target | Test Gate |
|--------|--------|-----------|
| `import monkeybot` | < 200ms | `test_cold_start.py` (CI block) |
| `python -m monkeybot --help` | < 500ms | `test_cold_start.py` (CI block) |
| First token from Gemini | < 2s | Manual smoke test |
| Tool dispatch overhead | < 5ms per call | Unit test with FakeProvider |
| `history.load()` — 100 messages | < 50ms | SQLite index guarantees this |
| `search_memory()` — 50 files | < 100ms | Sync scan, no I/O bottleneck |
| `load_turn_context()` | < 20ms | Reads ≤3 files + directory scan |

### Import Budget Enforcement

The 200ms cold start budget is allocated as follows:

| Module | Allocated | Enforcement |
|--------|-----------|-------------|
| Python interpreter startup | ~80ms | Fixed cost |
| `click` import | ~20ms | Required for CLI |
| `aiosqlite` import | ~15ms | Async SQLite |
| `pydantic` import | ~25ms | Config only |
| All `monkeybot.*` modules | ~60ms | Enforced by CI test |
| **Total** | **~200ms** | **CI gate** |

**The golden rule:** No `import google.generativeai` at module top level — ever. This import alone takes ~150ms. It must be inside `GeminiProvider.stream()`.

Same applies to `anthropic`, `openai`, and any other LLM SDK.

**How to verify locally:**
```bash
python -c "
import time, subprocess
for mod in ['google.generativeai', 'anthropic', 'openai']:
    t = time.monotonic()
    subprocess.run(['python', '-c', f'import {mod}'], capture_output=True)
    print(f'{mod}: {(time.monotonic() - t)*1000:.0f}ms')
"
```

### Async Patterns

**Rule:** All I/O must be non-blocking. No `time.sleep()`, no `open()` in async context.

```python
# CORRECT: sync filesystem ops wrapped in asyncio.to_thread
result = await asyncio.to_thread(read_file, path)
result = await asyncio.to_thread(write_file, path, content)
result = await asyncio.to_thread(search_memory, query, memory_path)
result = await asyncio.to_thread(list_skills, skills_path)

# CORRECT: subprocess via asyncio
proc = await asyncio.create_subprocess_shell(command, ...)

# CORRECT: SQLite via aiosqlite
async with aiosqlite.connect(db_path) as db:
    await db.execute(...)
```

**`run_command` is the one exception** — it's already `async` via `asyncio.create_subprocess_shell`.

### Scalability Constraints (E1 scope)

MonkeyBot v2 is a single-bot, single-process framework in E1. Scaling characteristics:

| Dimension | E1 Behaviour | Future Path |
|-----------|-------------|------------|
| Concurrent users | 1 (CLI is interactive) | Google Chat gateway (E2) handles concurrent webhooks |
| Context window | Model limit (~1M tokens for Gemini 2.0 Flash) | Summarization + memory injection replaces raw history |
| Memory files | < 50 files (full scan) | Cached index if > 50 files (later epic) |
| SQLite writes | Sequential (no concurrency issue) | Multiple gateway instances need shared volume mount |
| Subagents | Not in E1 | E4: `subagent_proto.py` spawns child processes |

The framework is stateless between turns — context is loaded fresh each turn from disk. This makes horizontal scaling via multiple containers natural: mount the same `/data` volume and any instance can serve any session.

---

## Deployment Strategy

### Docker

**Dockerfile (multi-stage, minimal):**

```dockerfile
# Stage 1: build
FROM python:3.11-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev

# Stage 2: runtime
FROM python:3.11-slim
WORKDIR /app

# Runtime deps only (no build tools)
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY bots/ ./bots/

# Data directory — mount a volume here
RUN mkdir -p /data/memory

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV DB_URL="sqlite:////data/monkeybot.db"
ENV MEMORY_PATH="/data/memory"

# Non-root user
RUN adduser --disabled-password --gecos "" monkeybot
USER monkeybot

ENTRYPOINT ["python", "-m", "monkeybot"]
```

**Image size target:** < 200MB (slim base + minimal extras). No build tools, no dev dependencies, no LLM SDKs beyond the selected provider.

**docker-compose.yml (local dev):**

```yaml
services:
  monkeybot:
    build: .
    env_file: .env
    volumes:
      - ./data:/data
      - ./bots:/app/bots
    stdin_open: true
    tty: true
    command: ["run", "--bot-dir", "/app/bots/example-bot"]
```

### Cloud-Agnostic Deployment

MonkeyBot is a Docker container. It runs identically on:

| Platform | Volume mount for `/data` | Notes |
|----------|--------------------------|-------|
| Local Docker | `./data:/data` bind mount | Default |
| AWS ECS/Fargate | EFS mount | Persistent storage across task restarts |
| GCP Cloud Run | GCS FUSE or Cloud Run volume | Testing target |
| Azure Container Apps | Azure File Share | SMB mount |
| Kubernetes (any cloud) | PersistentVolumeClaim | Standard K8s pattern |
| Self-hosted Docker | Local bind mount | Same as local dev |

**No cloud SDK is required for any of these.** The operator provides the volume — MonkeyBot treats it as a filesystem.

### Deployment Pipeline

```
git push
    │
    ▼
CI: ruff + mypy + pytest (unit)
    │ pass
    ▼
CI: docker build + test_cold_start.py
    │ pass
    ▼
CI: pytest integration (FakeProvider + real SQLite)
    │ pass
    ▼
CI (on tag): docker push → registry
    │
    ▼
Deploy: docker pull + restart container
    │
    ▼
Smoke test: monkeybot --help exits 0 + import time check
```

### Rollback Plan

| Trigger | Action | Time Target |
|---------|--------|-------------|
| Import time regression (> 200ms) | CI blocks merge — never reaches prod | Instant |
| Runtime error spike | `docker stop` + `docker start {previous_image}` | < 2 minutes |
| DB schema issue | `DB_URL` points to backup copy of SQLite file | < 1 minute |
| Bad bot config | Swap `AGENT_MD_PATH` env var + restart | < 1 minute |

Rollback is simple because the state is in a single SQLite file and memory directory. Restoring a previous image + pointing at a backup of `/data` is a complete rollback.

### Health Checks

**CLI tool:** No HTTP health endpoint in E1 (no server). Health is verified by:
```bash
python -c "import monkeybot; print('OK')"      # Import health
python -m monkeybot --help                      # CLI health
```

**When the Google Chat gateway ships (E2):** `/health` → HTTP 200 when DB connection + provider available.

---

## Observability

### Structured Logging

Python's stdlib `logging` module with a JSON formatter. No external logging SDK in E1.

**Log format:**
```json
{
  "timestamp": "2026-05-13T18:00:00.123Z",
  "level": "INFO",
  "service": "monkeybot",
  "session_id": "01J...",
  "run_id": "01J...",
  "event": "turn_complete",
  "input_tokens": 1024,
  "output_tokens": 256,
  "cost_usd": 0.0003,
  "duration_ms": 1850,
  "message": "Turn complete"
}
```

**Log levels:**
| Level | When |
|-------|------|
| `DEBUG` | Tool args, provider raw events (disabled in prod via `LOG_LEVEL`) |
| `INFO` | Turn start, tool calls, turn complete with token counts |
| `WARNING` | Inspector deny, tool timeout, missing optional config |
| `ERROR` | Provider stream failure, DB write failure |
| `CRITICAL` | Agent loop panic (unhandled exception in run()) |

**What to log:**
- Turn start: `session_id`, `run_id`, message length (not content)
- Tool calls: `tool_name`, `duration_ms`, `exit_code` (for run_command)
- Turn complete: `input_tokens`, `output_tokens`, `cost_usd`, `duration_ms`
- Errors: type, message, `session_id`, `run_id` — never stack frames with secrets

**What NOT to log:**
- Full conversation content (contains PII from user messages)
- `GEMINI_API_KEY` or any env var value
- Tool `command` argument (may contain secrets passed as args)
- Full memory file contents

**JSON logging setup (in `cli.py`):**
```python
import logging, json, sys

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "service": "monkeybot",
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log.update(record.extra)
        return json.dumps(log)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(handlers=[handler], level=os.getenv("LOG_LEVEL", "INFO"))
```

### Metrics (E1 Scope — Lightweight)

Full metrics (Prometheus/OpenTelemetry) are E3 scope. In E1, `TurnComplete` events carry the key counters:

| Field in TurnComplete | What it measures |
|-----------------------|-----------------|
| `input_tokens` | Model input cost |
| `output_tokens` | Model output cost |
| `cost_usd` | Estimated turn cost |
| `duration_ms` | Total turn wall time |

These are also written to the log in structured form — sufficient for manual cost review and performance baselining. The `monkeybot usage` CLI command (E3) will aggregate from the DB.

### Tracing (E1 Scope)

No distributed tracing in E1. `run_id` (ULID) in every log line provides manual correlation:

```bash
# Trace a single turn manually
grep '"run_id": "01JV..."' /var/log/monkeybot.log | jq .
```

Full OpenTelemetry tracing is E3 scope.

### Alerting (E1 Scope)

E1 is a local CLI tool — no alerting infrastructure. When the Google Chat gateway ships (E2), these are the trigger conditions to implement:

| Condition | Alert |
|-----------|-------|
| Provider stream returns zero text + zero tool calls | Log ERROR + ErrorEvent |
| Tool call denied by inspector | Log WARNING with tool_name |
| Turn duration > 30s | Log WARNING |
| `run_command` exit code ≠ 0 | Log WARNING with exit_code |

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation | E1 or Later |
|------|-----------|--------|------------|-------------|
| LLM issues destructive `run_command` via prompt injection | Medium | High | `RulesInspector` + denied pattern list from config.yaml; full HITL in E2 | E1 partial, E2 full |
| `read_file` path traversal exposes `.env` or SSH keys | Medium | High | `allowed_roots` enforcement in tool dispatcher | E1 |
| `GEMINI_API_KEY` leaked via log line | Low | High | JSON formatter never logs env vars; API key only accessed inside `stream()` | E1 |
| Cold start regression breaks CI | Medium | Low | `test_cold_start.py` blocks merge on regression; lazy import discipline | E1 (CI gate) |
| LLM SDK breaking change (google-genai API) | Medium | Medium | SDK isolated to `providers/gemini.py`; swapping provider is one file | E1 design |
| Context window overflow (very long sessions) | Medium | Medium | History grows unbounded in E1; summarization strategy in E3 | E3 |
| SQLite DB corruption on abrupt container kill | Low | Medium | SQLite WAL mode enabled; `/data` on durable volume; worst case: last turn lost | E1 (WAL mode) |
| `write_file` tool allows overwriting `AGENT.md` or framework files | Low | High | `write_file` write roots restricted to `memory_path` + `bot_dir/output` | E1 |
| Model hallucinates a tool name not in the 5-tool registry | Low | Low | Unknown tool name → `"Unknown tool: {name}"` string returned to model | E1 (handled) |
| Gemini API rate limit / quota exhaustion | Low | Medium | `ProviderDone` with ErrorEvent on API error; no retry in E1 | E1 (accept + log) |

### Risk Monitoring

- `run_command` denial rate tracked in INFO logs — spike suggests adversarial prompting.
- Cold start times tracked in CI over time — regression visible in PR history.
- Manual cost review via `TurnComplete.cost_usd` log aggregation until E3 usage dashboard.

---

## Implementation Notes for E1

### SQLite WAL Mode

Enable WAL (Write-Ahead Logging) in `ConversationHistory.init()` to survive abrupt kills:

```python
async def init(self) -> None:
    async with aiosqlite.connect(self._db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(CREATE_MESSAGES_TABLE_SQL)
        await db.execute(CREATE_MESSAGES_INDEX_SQL)
        await db.commit()
```

`synchronous=NORMAL` is the right tradeoff: safe against OS crashes, not against power loss (acceptable for this use case).

### `__init__.py` Must Stay Empty

`src/monkeybot/__init__.py` exports the public API but must not eagerly import heavy dependencies:

```python
# src/monkeybot/__init__.py
# Public API — lazy imports keep cold start fast
from __future__ import annotations

def __getattr__(name: str):  # type: ignore[override]
    if name == "AgentLoop":
        from monkeybot.core.loop import AgentLoop
        return AgentLoop
    if name == "ConversationHistory":
        from monkeybot.core.history import ConversationHistory
        return ConversationHistory
    raise AttributeError(f"module 'monkeybot' has no attribute {name!r}")

__version__ = "2.0.0"
```

This means `import monkeybot` does zero work beyond registering the lazy loader — the 200ms budget is achievable.

### `scripts/bootstrap` Update

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "Installing dependencies..."
uv sync --extra gemini --extra dev

echo "Copying env template..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "→ Edit .env and set GEMINI_API_KEY before running"
fi

echo "Creating data directories..."
mkdir -p data/memory

echo "Bootstrap complete."
echo "Run: scripts/run"
```

### Docker Build Target

Build with only the selected provider extra to keep image minimal:

```bash
# Gemini only (default):
docker build --build-arg EXTRAS="gemini" -t monkeybot:latest .

# Claude only:
docker build --build-arg EXTRAS="claude" -t monkeybot:latest .
```

Provider extras are never installed together by default — no reason to bundle all LLM SDKs.

---

## Final Sign-Off Checklist

- [x] Security: `run_command` injection defense via `RulesInspector` + `denied_patterns`
- [x] Security: `read_file`/`write_file` path traversal prevention via `allowed_roots`
- [x] Security: API key redaction from subprocess environment
- [x] Security: Zero cloud SDK in core — cloud-agnostic Docker image
- [x] Performance: Import budget defined and CI-enforced (200ms hard gate)
- [x] Performance: Lazy LLM SDK import — google-genai inside `stream()` only
- [x] Performance: SQLite index on `(session_id, created_at)` for fast history load
- [x] Performance: All I/O async — sync tools wrapped in `asyncio.to_thread`
- [x] Deployment: Multi-stage Dockerfile, non-root user, slim base image
- [x] Deployment: Cloud-agnostic via volume mount for `/data`
- [x] Deployment: SQLite WAL mode for crash safety
- [x] Observability: Structured JSON logging, LOG_LEVEL configurable
- [x] Observability: `run_id` (ULID) in every log line for manual correlation
- [x] Observability: `TurnComplete` carries tokens + cost for manual usage review
- [x] Risk: All HIGH-impact risks have E1 mitigations or explicit E2 deferrals
- [x] Risk: `test_cold_start.py` CI gate prevents import time regressions
