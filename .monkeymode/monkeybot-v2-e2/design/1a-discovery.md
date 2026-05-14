# Design: monkeybot-v2-e2 — Safety, Skills & Production Gateway
## Phase 1A: Discovery & Core Design

---

## Executive Summary

E2 makes the E1 harness production-ready by wiring a YAML-driven safety inspector chain into `AgentLoop`, populating the skills directory with 4 built-in skills, adding a full `ClaudeProvider`, and shipping a platform-agnostic `WebhookGateway` + Docker base image. A bot operator can deploy to any container host (AWS, GCP, Azure, Fly.io, etc.) and connect to any webhook-capable chat platform (Google Chat, Slack, Teams, Discord, etc.) — no framework changes required.

---

## Use Case & Business Value

| Stakeholder | Pain Today | After E2 |
|-------------|-----------|----------|
| Bot developer | Safety config requires Python edits | Declare tiers in `config.yaml`, no code changes |
| Skill author | Must write Python to add capabilities | Drop a `SKILL.md` file — bot discovers it automatically |
| Bot operator | No production deploy path | `docker build` produces a portable image; deploy anywhere |
| Platform integrator | Framework tied to one chat platform | Drop a `webhook.py` in the bot dir to support any platform |

**Out of scope for E2:**
- Built-in web/search skill (bring-your-own search API via SKILL.md)
- Platform-specific deploy scripts (user-land; example bot documents patterns)
- OpenAI provider (E3+)
- Scheduled jobs / cron (E3)

---

## Architecture Decision

### Chosen Approach: Thin layer over E1 — no new runtime components

E2 adds to the existing module tree. `AgentLoop`, `Provider`, and the tool registry from E1 are unchanged; E2 wraps them with:

1. **`core/safety.py`** — `load_inspectors()` factory reads `config.yaml` once at startup, returns `list[ToolInspector]`.
2. **`gateway/webhook.py`** — generic FastAPI app (`POST /webhook`, `GET /health`) with a pluggable `MessageExtractor` callable. Platform adapters live in user-land bot directories, not in the framework.
3. **4 built-in skills** as pure-markdown files under `.agents/skills/`. No Python required.
4. **`providers/claude.py`** — full streaming `ClaudeProvider` using the `anthropic` SDK.
5. **`providers/_utils.py`** — shared `estimate_cost()` utility; removes duplication between providers.
6. **`docker/Dockerfile`** — framework base image only (package + deps, no bot dir baked in). Users extend it.

### Why generic webhook, not Google-Chat-specific?

The framework ships a container. Where that container runs and which chat platform hits its `/webhook` endpoint is entirely up to the operator. Tying the framework to Google Chat's payload format would:
- Make every Slack/Teams/Discord user patch framework code
- Couple image versioning to platform API changes
- Imply Cloud Run as the target deployment

Instead, the bot dir owns platform logic via a single file (`webhook.py`). The framework never sees a Google Chat payload — only a plain string extracted from it.

### Alternatives Considered

| Option | Pros | Cons | Why Not Chosen |
|--------|------|------|----------------|
| Google-Chat-specific `GoogleChatGateway` | Less user config | Ties framework to one platform | Wrong abstraction level |
| Separate gateway microservice | Independent scaling | Two processes, two configs | Over-engineering for single-bot MVP |
| **Chosen: generic `WebhookGateway` + pluggable extractor** | Works with any platform; platform logic stays in bot dir | User writes one extractor file | Correct level of abstraction |

---

## Architecture Diagram

```
Any Chat Platform            Any Container Host
(Google Chat / Slack         (AWS ECS / GCP Cloud Run /
 / Teams / Discord)           Fly.io / self-hosted)
        │                              │
        │ HTTPS POST /webhook          │
        ▼                              ▼
┌───────────────────────────────────────────────────┐
│  Docker Container  (monkeybot base image)         │
│                                                   │
│  FastAPI  (uvicorn)    gateway/webhook.py         │
│  POST /webhook ──▶ extract_message(payload)       │
│                    [from bot's webhook.py]        │
│  GET  /health  ──▶ {"status": "ok"}               │
│           │                                       │
│           ▼  user_message str                     │
│  ┌────────────────────────────────────────────┐   │
│  │  AgentLoop  (E1, unchanged)                │   │
│  │  ├── Provider  (Gemini or Claude)          │   │
│  │  ├── ToolInspector chain  ◀── config.yaml  │   │
│  │  │     CommandTierInspector               │   │
│  │  │     RulesInspector                     │   │
│  │  └── 5 tools + list_skills                │   │
│  └────────────────────────────────────────────┘   │
│           │                                       │
│           ▼  response str                         │
│  format_response(text) ──▶ platform JSON body     │
│  [from bot's webhook.py — default: {"text": text}]│
└───────────────────────────────────────────────────┘
```

```
Bot Directory Structure (user-owned, mounted at /bot)
─────────────────────────────────────────────────────
/bot/
├── AGENT.md           # System prompt / persona
├── config.yaml        # Safety tiers, model config
├── webhook.py         # Platform extractor (user writes this)
│                      # extract_message(payload) -> str | None
│                      # format_response(text) -> dict  (optional)
└── .agents/skills/    # Any SKILL.md files
```

---

## Platform Extractor Pattern

The framework dynamically loads `{bot_dir}/webhook.py` at startup. This file defines one required function and one optional function:

```python
# bot-dir/webhook.py — user writes this per platform

def extract_message(payload: dict) -> str | None:
    """Return the user's message text, or None to ignore this event."""
    ...

def format_response(text: str) -> dict:
    """Return the JSON body to send back to the platform."""
    ...   # optional — default: {"text": text}
```

**If `webhook.py` is absent**, the gateway falls back to a generic extractor that checks common text fields: `payload.get("text") or payload.get("message", {}).get("text")` — works for simple webhooks and local testing.

The example bot ships two reference implementations:

```
bots/example-bot/
├── webhook.py                   # Google Chat extractor (reference)
├── webhook_slack_example.py     # Slack extractor (reference, not loaded by default)
└── ...
```

These are reference files for users — they are NOT part of the installed framework package.

---

## Core Data Model

### No new persistent entities

E2 reuses `ConversationHistory` (SQLite via `aiosqlite`) from E1. The only new "data" is config shapes:

#### `config.yaml` safety block

```yaml
safety:
  command_tiers:
    pre_approved:    [read_file, list_skills, search_memory]
    requires_approval: [write_file]
    denied:          [run_command]
  denied_patterns:
    - "rm -rf"
    - "/etc/passwd"
```

Runtime type (in `core/safety.py`): parsed into `CommandTierInspector` + `RulesInspector` instances — not persisted.

#### Webhook payload (platform-specific — not owned by framework)

The framework receives `dict[str, Any]` from FastAPI's JSON body parser and passes it to `extract_message()`. The shape is entirely defined by the user's extractor. The framework never inspects it.

---

## Key Design Decisions

### ADR-E2-001: `load_inspectors()` in `core/safety.py`, not `cli.py`

**Status:** Accepted  
**Decision:** Centralise in `core/safety.py` so both the CLI gateway and the webhook gateway use the same inspector chain. `cli.py`'s `_load_inspectors()` becomes a one-liner delegation.  
**Consequences:** Consistent safety behaviour across all gateways; single place to test.

### ADR-E2-002: `ClaudeProvider` is a full streaming implementation

**Status:** Accepted  
**Decision:** Complete streaming + tool use implementation using the `anthropic` SDK. `ANTHROPIC_API_KEY` validated at `__init__` (fail-fast). Default model: `claude-3-5-sonnet-20241022`. Stubs that ship never get finished.  
**Consequences:** `MODEL_PROVIDER=claude` fully routes to Claude. Unit tests mock `AsyncClient`.

### ADR-E2-003: No base provider class — shared `_utils.py` instead

**Status:** Accepted  
**Decision:** Extract `estimate_cost()` to `providers/_utils.py`. Both providers import it. `Provider` Protocol remains the interface. No class hierarchy.  
**Consequences:** Zero coupling between provider implementations; trivial to add a third provider.

### ADR-E2-004: Webhook gateway is platform-agnostic; extractor lives in bot dir

**Status:** Accepted  
**Decision:** `gateway/webhook.py` is generic. Platform logic (payload parsing, response formatting) lives in `{bot_dir}/webhook.py`, owned by the user. Example bot ships two reference extractors (Google Chat, Slack).  
**Consequences:** Framework never imports platform SDKs. Bot operators are never forced to change framework code when switching platforms.

### ADR-E2-005: Docker base image contains framework only; bot dir is mounted/extended

**Status:** Accepted  
**Decision:** `docker/Dockerfile` installs the monkeybot package and extras. It does NOT COPY any bot directory. Users extend the base image or volume-mount their bot dir.  
**Consequences:** Framework image is reusable across any bot. Deploy scripts are user-land (example bot provides `deploy-aws.sh` and `deploy-gcp.sh` as reference, not framework artefacts).

### ADR-E2-006: `research-web` skill deferred — bring-your-own search

**Status:** Accepted  
**Decision:** No built-in search skill in E2. Web search depends on which API the user wants (Google, Perplexity, Brave, etc.). The skill system exists precisely to let users bring their own. Document the pattern; ship it when there's a clear default.  
**Consequences:** E2 ships 4 built-in skills. `research-web` is a documented "add your own" example in the example bot.

### ADR-E2-007: Webhook token verification is opt-in

**Status:** Accepted  
**Decision:** If `WEBHOOK_SECRET` env var is set, verify `Authorization: Bearer {token}` using a pluggable `verify_token(token, secret) -> bool` function. Default: HMAC-SHA256 (works for Slack, GitHub webhooks). For Google Chat's Google-signed tokens, users override in `webhook.py`.  
**Consequences:** Works out of the box for most platforms; extensible for platform-specific auth.

---

## Build Order

1. `providers/_utils.py` — `estimate_cost()`; update `GeminiProvider` to use it
2. `providers/claude.py` — full `ClaudeProvider`
3. `core/safety.py` — `load_inspectors()` factory; update `cli.py`
4. `bots/example-bot/config.yaml` — full safety config
5. `.agents/skills/` — 4 built-in skills: `memory-save`, `memory-search`, `file-ops`, `self-improve`
6. `gateway/webhook.py` — generic `WebhookGateway`
7. `cli.py` — add `monkeybot serve` command
8. `docker/Dockerfile` + `docker/docker-compose.yml` — framework base image
9. `bots/example-bot/webhook.py` — Google Chat extractor (reference)
10. `bots/example-bot/webhook_slack_example.py` — Slack extractor (reference)
11. `tests/unit/test_safety.py`, `tests/unit/test_claude_provider.py`
12. `tests/integration/test_gateway.py`

---

## Files to Create / Modify

### New files
| File | Owner | Purpose |
|------|-------|---------|
| `src/monkeybot/providers/_utils.py` | Framework | Shared `estimate_cost()` |
| `src/monkeybot/providers/claude.py` | Framework | Full ClaudeProvider |
| `src/monkeybot/core/safety.py` | Framework | `load_inspectors()` factory |
| `src/monkeybot/gateway/webhook.py` | Framework | Generic `WebhookGateway` |
| `.agents/skills/memory-save/SKILL.md` | Framework | Built-in skill |
| `.agents/skills/memory-search/SKILL.md` | Framework | Built-in skill |
| `.agents/skills/file-ops/SKILL.md` | Framework | Built-in skill |
| `.agents/skills/self-improve/SKILL.md` | Framework | Built-in skill |
| `docker/Dockerfile` | Framework | Base image (package only) |
| `docker/docker-compose.yml` | Framework | Local dev with volume mount |
| `bots/example-bot/webhook.py` | Example | Google Chat extractor reference |
| `bots/example-bot/webhook_slack_example.py` | Example | Slack extractor reference |
| `tests/unit/test_safety.py` | Tests | Inspector chain |
| `tests/unit/test_claude_provider.py` | Tests | ClaudeProvider (mocked) |
| `tests/unit/test_utils.py` | Tests | estimate_cost() |
| `tests/integration/test_gateway.py` | Tests | Webhook smoke tests |

### Modified files
| File | Change |
|------|--------|
| `src/monkeybot/providers/gemini.py` | Use `_utils.estimate_cost()` instead of local function |
| `src/monkeybot/cli.py` | Add `serve` command; delegate `_load_inspectors()` to `core/safety.py`; update provider factory |
| `bots/example-bot/config.yaml` | Add `safety.command_tiers` + `safety.denied_patterns` |
| `src/monkeybot/__init__.py` | Export `WebhookGateway` if `gchat` extras present |

---

## Next Steps

- **Phase 1B:** API contracts — `WebhookGateway` interface, `load_inspectors()` signature, `ClaudeProvider` streaming contract, Docker build contract
- **Phase 1C:** Security (WEBHOOK_SECRET verification), performance (uvicorn workers, async extractor), observability
