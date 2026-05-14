# Q&A Log — monkeybot-v2-e1

## Session: 2026-05-13

### MonkeyMode Setup
**Q: Would you like to save a Q&A log?**  
A: Yes

**Q: Where should MonkeyMode start — Phase 1 (Design) or skip to Phase 3 (Code Spec)?**  
A: Start at Phase 1 (Design) — full MonkeyMode flow

---

### Phase 1A Design Decisions
**Q: What architecture approach for the agent loop?**  
A: OS-Kernel analogy with typed dataclasses (Approach C). Rejected LangChain (import time), rejected Pydantic in hot path (overhead).

**Q: Events: Pydantic or dataclasses?**  
A: `@dataclass` — stdlib, zero overhead. Pydantic reserved for config/data models at boundaries.

**Q: Storage: SQLite or PostgreSQL?**  
A: SQLite (aiosqlite). Zero infrastructure. `/data` is a generic mount point — any cloud volume or local bind mount works. DB_URL is an env var; operators can switch to PostgreSQL DSN without code changes.

**Q: Cloud provider dependency?**  
A: Zero cloud SDKs in `src/monkeybot/core/`. Framework runs on GCP, AWS, Azure, or plain Docker. GCP is used for testing only — nothing in the codebase may import a GCP SDK.

**Q: How many tools?**  
A: Exactly 5, hard-coded in loop.py. run_command is the escape hatch. No dynamic registration.

**Q: Provider extensibility approach?**  
A: `@runtime_checkable Protocol` — structural subtyping. No base class required.

---

### Phase 1B Contract Decisions
**Q: Error handling strategy for tool functions?**  
A: Return sentinel string (tools), raise ValueError at init, yield ErrorEvent at runtime. Loop never raises into gateway.

**Q: Sync or async tool functions?**  
A: Sync (disk I/O only) → called via asyncio.to_thread in loop. Only run_command is natively async.

**Q: Import strategy for LLM SDKs (cold start)?**  
A: Lazy import inside stream() method only. google-genai must NOT be at module top level. Budget: providers/gemini.py < 5ms.

**Q: What events are emitted in E1 vs later epics?**  
A: E1 emits: UserMessage, AssistantDelta, ToolCallStarted, ToolCallResult, TurnComplete, ErrorEvent. ApprovalRequest/Response (E2), SubagentStarted/Completed (E4).

**Q: Public module surface stability?**  
A: Stable: events, loop, history, provider, context. Internal/unstable: providers/, gateway/, anything prefixed _.

---

### Phase 1C Operations Decisions
**Q: Biggest security risk?**  
A: run_command — LLM controls shell args. Mitigated by RulesInspector + denied_patterns in config.yaml. Full HITL in E2.

**Q: read_file/write_file path traversal?**  
A: allowed_roots enforcement. Default roots: bot_dir, memory_path, skills_path. Anything outside returns "ERROR: Access denied".

**Q: Should API keys be redacted from subprocess environment?**  
A: Yes. _safe_env() strips GEMINI_API_KEY, DB_URL, and other secrets before passing env to subprocess.

**Q: SQLite crash safety?**  
A: WAL mode + synchronous=NORMAL. Safe against OS crashes. Last-turn loss on power failure is acceptable.

**Q: Lazy import rule for LLM SDKs?**  
A: google-genai (and all LLM SDKs) must be imported INSIDE stream() only. Module-level import alone costs ~150ms — kills the 200ms cold start budget.

**Q: Metrics/tracing scope for E1?**  
A: Lightweight only. TurnComplete carries tokens+cost. run_id (ULID) in all log lines for manual correlation. Full OpenTelemetry is E3.

---
