# Q&A Log — monkeybot-v2-e2

## Phase 1A — Discovery & Core Design

**2026-05-13**

**Q: Was Q&A logging requested for this epic?**  
A: Yes — inheriting from E1 preference (`save_qa_log: true`).

**Q: What existing code from E1 does E2 build on?**  
A: `CommandTierInspector`, `RulesInspector`, `ToolInspector` Protocol all exist in `core/inspector.py`. `list_skills()` already scans `.agents/skills/*/SKILL.md`. `AgentLoop` already accepts `inspectors: list[ToolInspector]`. `pyproject.toml` already has `gchat` optional extras (`fastapi`, `uvicorn`, `google-auth`). `cli.py` has a private `_load_inspectors()` that only wires `RulesInspector` — E2 upgrades it to use a shared `core/safety.py` factory.

**Q: Where does `load_inspectors()` live — `core/safety.py` or `cli.py`?**  
A: `core/safety.py` (ADR-E2-001). Both `cli.py` and the new `GoogleChatGateway` need the same inspector chain; centralising avoids duplication.

**Q: ClaudeProvider — working stub or full implementation?**  
A: Full implementation (ADR-E2-002 revised). Stubs that ship never get finished. `ClaudeProvider` is a complete streaming + tool use provider using the `anthropic` SDK, matching the same `Provider` Protocol as `GeminiProvider`. Missing `ANTHROPIC_API_KEY` raises immediately at startup, not lazily on first call.

**Q: Should Google Chat signature verification be mandatory in dev?**  
A: Opt-in via `GOOGLE_CHAT_AUDIENCE` env var (ADR-E2-003). Absent → skip + log warning (dev mode). Present → verify with `google-auth`.

**Q: How does `research-web` skill handle web search without a new API key?**  
A: `search.py` uses DuckDuckGo Instant Answer API via `httpx` — no API key required. Agent calls it via `run_command` (ADR-E2-004).

## Phase 1B — Detailed Contracts

**2026-05-13**

**Q: Should there be a base provider class to avoid duplication across GeminiProvider and ClaudeProvider?**  
A: No base class. Only real duplication is the cost estimation formula and a boolean property. Extract `estimate_cost()` to `providers/_utils.py`; both providers import it. Protocol stays as the interface. Inheritance coupling not worth it for 2-3 providers.

**Q: Should the gateway be Google Chat-specific or platform-agnostic?**  
A: Platform-agnostic (ADR-E2-004). `WebhookGateway` accepts raw JSON and a pluggable `extract_message()` callable. Platform logic lives in `{bot_dir}/webhook.py` — user-owned, not framework code. Example bot ships Google Chat and Slack reference extractors.

**Q: Should Cloud Run / deploy scripts be part of the framework?**  
A: No. Docker image is the deploy artifact. Where it runs is operator choice. `deploy.sh` is user-land. Example bot README links to deploy guides for AWS ECS, GCP Cloud Run, Fly.io, etc.

**Q: What's the session ID strategy?**  
A: Derived by user's `session_id(payload)` function in `webhook.py`. Google Chat reference: `f"{space.name}/{sender.name}"`. Slack reference: `f"{channel}/{user}"`. Fallback: new ULID per request.

**Q: Are `fastapi`/`uvicorn` core deps or optional?**  
A: Optional `[gchat]` extras — already in `pyproject.toml` from E1. The `monkeybot serve` command fails with a clear import error if extras not installed.

**Q: What's the inspector chain order?**  
A: `CommandTierInspector` first (tool name tier check), then `RulesInspector` (argument pattern check). First non-allow wins.

**Q: Should `research-web` be a built-in skill?**  
A: Deferred (ADR-E2-006). Users choose their own search API. The skill system is the mechanism — drop a `SKILL.md`. E2 ships 4 built-in skills without search.

## Phase 1C — Production Readiness

**2026-05-13**

**Q: How should webhook authentication work across different platforms?**  
A: HMAC-SHA256 via `WEBHOOK_SECRET` env var — covers Slack, GitHub, and most REST webhook platforms natively. Google Chat uses Google-signed JWTs; users handle that in their `extract_message()`. Framework stays platform-agnostic.

**Q: Should `/health` probe the LLM API and SQLite on every call?**  
A: No — liveness only. Probing the LLM on every load balancer ping wastes quota and adds latency. A `/ready` endpoint can be added in a later epic if needed.

**Q: Should the Dockerfile use one stage or multi-stage?**  
A: Multi-stage (upgrade from E1 draft). Builder stage installs deps; runtime stage copies only the installed packages + source. Non-root user enforced. `EXTRAS` build arg selects providers at build time.

**Q: Should the `/bot` mount be read-only in docker-compose?**  
A: Yes. The container should not modify bot configuration at runtime. Read-only mount makes this constraint explicit.
