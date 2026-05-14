# Design: MonkeyBot v2 E1 — Core Harness & Walking Skeleton
## Phase 1A: Discovery & Core Design

---

## Executive Summary

MonkeyBot v2 is a ground-up Python agent framework with a deliberately thin kernel (~500 LOC). The harness acts as an OS: a typed event loop dispatching five deterministic tools, with intelligence delegated entirely to markdown skills and the LLM. E1 delivers the walking skeleton — a locally runnable agent with streaming responses, persistent history, and file-based memory.

---

## Use Case & Business Value

**Who:** Bot developers building and deploying LLM-driven bots on Google Chat / Cloud Run.

**What:** A framework that eliminates boilerplate. A developer clones the repo, drops an `AGENT.md`, sets `GEMINI_API_KEY`, and has a working interactive agent in minutes. No cloud setup required for local dev.

**Why now:** The v1 codebase accumulated complexity. v2 is a clean slate with explicit constraints: ≤6 hard dependencies, harness ≤500 LOC, 5 tools maximum, zero cloud SDKs in core. These constraints are non-negotiable.

**Success metrics (E1):**
- `import monkeybot` < 200ms (CI-gated)
- `python -m monkeybot --help` < 500ms (CI-gated)
- `monkeybot run` completes a 3-turn conversation with persistent history
- `ruff check src/` and `mypy --strict src/` both clean

**Out of scope for E1:** Safety/HITL, Google Chat gateway, scheduler, subagents, LLM council, usage tracking, providers other than Gemini.

---

## Technical Context

| Dimension | Decision |
|-----------|----------|
| Python | 3.11 (pyproject.toml confirmed) |
| Package manager | uv |
| LLM SDK | google-genai >= 0.8 (primary); anthropic, openai as optional extras |
| Async SQLite | aiosqlite >= 0.20 |
| Validation | pydantic >= 2.0 (config/models; NOT in the hot event stream) |
| CLI | click >= 8.0 |
| IDs | ulid-py >= 1.1 |
| Test runner | pytest 8 + pytest-asyncio (asyncio_mode="auto") |
| Linting | ruff, mypy --strict |
| Deployment target | Docker (cloud-agnostic — GCP/AWS/Azure/self-hosted) |
| Storage | markdown files (memory) + SQLite (history, runs) |
| Cloud SDKs in core | 0 — no cloud SDK imports in `src/monkeybot/core/` |

---

## Architecture Decision

### The Three Competing Approaches

#### Approach A: LangChain / LangGraph-based
**Pros:** Ecosystem, built-in tool abstractions, observability integrations.  
**Cons:** Heavy import tree (violates <200ms cold start), opaque internals, upgrade churn, vendor lock-in on abstractions.  
**Verdict:** ❌ Rejected — import time alone disqualifies it.

#### Approach B: Thin framework with Pydantic BaseModel events
**Pros:** Pydantic validation, IDE autocomplete, schema export.  
**Cons:** Pydantic in the hot streaming path adds ~30ms import overhead; `model_dump()` slower than `dataclasses.asdict()` for high-volume event serialization.  
**Verdict:** ❌ Rejected for events — Pydantic stays in config/data models, not the hot path.

#### Approach C: OS-Kernel analogy with typed dataclasses ✅ CHOSEN
**Description:**
- Harness = OS kernel: runs the loop, manages context, dispatches tools
- Tools = syscalls: deterministic, five max, `run_command` is the escape hatch
- Skills = user programs: markdown files loaded on demand
- Events = typed dataclasses: zero-overhead serialization, `AsyncIterator[AgentEvent]`
- Storage = filesystem: markdown + SQLite, no infrastructure required

**Pros:**
- Cold start < 200ms achievable (dataclasses are stdlib)
- `FakeProvider` requires zero framework inheritance — pure structural subtyping
- Harness stays thin because all intelligence is in skills, not in the loop
- Files + SQLite run everywhere (local, Cloud Run, Docker)
- One file per new provider, one SKILL.md per new skill

**Cons:**
- No built-in RAG / vector search (intentional — `run_command` is the escape hatch)
- Requires manual schema maintenance for Provider Protocol additions

**Recommendation:** ✅ This is the architecture. The constraints (≤500 LOC kernel, 5 tools, ≤6 deps) enforce it.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  SKILLS  (Intelligence Layer)                               │
│  .agents/skills/{name}/SKILL.md                             │
│  Loaded by list_skills / read_file on demand               │
├─────────────────────────────────────────────────────────────┤
│  HARNESS  (The Kernel)                      ~500 LOC        │
│                                                             │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────┐    │
│  │ AgentLoop│  │ TurnContext   │  │ ConversationHistory│    │
│  │ loop.py  │  │ context.py   │  │ history.py (SQLite)│    │
│  └────┬─────┘  └──────────────┘  └───────────────────┘    │
│       │ AsyncIterator[AgentEvent]                           │
│       ▼                                                     │
│  ┌──────────────────────────────────────────────┐          │
│  │  AgentEvent stream (events.py)               │          │
│  │  10 typed dataclasses + JSON helpers         │          │
│  └──────────────────────────────────────────────┘          │
├─────────────────────────────────────────────────────────────┤
│  TOOLS  (Deterministic Layer)                               │
│  run_command · read_file · write_file                       │
│  search_memory · list_skills                                │
├─────────────────────────────────────────────────────────────┤
│  PROVIDERS  (Model Layer)                                   │
│  Provider Protocol — structural subtyping                   │
│  GeminiProvider (default) │ FakeProvider (tests)            │
├─────────────────────────────────────────────────────────────┤
│  GATEWAY  (I/O Layer — E1 scope: CLI only)                  │
│  CLIGateway: stdin → loop.run() → print deltas to stdout   │
├─────────────────────────────────────────────────────────────┤
│  STORAGE                                                    │
│  data/memory/*.md   — file-based memory                     │
│  data/monkeybot.db  — SQLite (history)                      │
└─────────────────────────────────────────────────────────────┘
                   ↕ Docker boundary
```

### Turn Sequence Diagram

```
CLIGateway          AgentLoop           GeminiProvider        SQLite
    │                   │                     │                  │
    │── user_msg ───────▶                     │                  │
    │                   │── history.load() ──────────────────────▶
    │                   │◀─ messages ─────────────────────────────
    │                   │── build_system_prompt()                 │
    │                   │── stream(messages, tools) ─────────────▶
    │                   │◀─ TextDelta ────────│                  │
    │◀── AssistantDelta ─                     │                  │
    │                   │◀─ ToolCall ─────────│                  │
    │◀── ToolCallStarted─                     │                  │
    │                   │── dispatch_tool()                       │
    │◀── ToolCallResult ─                     │                  │
    │                   │── stream(+ tool result) ───────────────▶
    │                   │◀─ TextDelta ────────│                  │
    │◀── AssistantDelta ─                     │                  │
    │                   │◀─ ProviderDone ─────│                  │
    │                   │── history.save() ──────────────────────▶
    │◀── TurnComplete ───                     │                  │
    │                   │                     │                  │
```

---

## Core Data Model

### AgentEvent (events.py)

The heartbeat of the system. Every meaningful action emits an event.

```
AgentEvent (Union of 10 types)
├── UserMessage         kind="user_message"     content, user_id, timestamp_ms
├── AssistantDelta      kind="assistant_delta"  text  (streaming chunk)
├── ToolCallStarted     kind="tool_call_started" call_id, tool_name, args
├── ToolCallResult      kind="tool_call_result"  call_id, tool_name, result, error, duration_ms
├── ApprovalRequest     kind="approval_request"  call_id, tool_name, args, reason  [E2]
├── ApprovalResponse    kind="approval_response" call_id, approved, approver_id   [E2]
├── SubagentStarted     kind="subagent_started"  run_id, script, parent_run_id    [E4]
├── SubagentCompleted   kind="subagent_completed" run_id, scratch_dir             [E4]
├── TurnComplete        kind="turn_complete"     run_id, tokens, cost_usd, duration_ms
└── ErrorEvent          kind="error"             message, recoverable
```

Serialization: `dataclasses.asdict()` → JSON. No external dependency. Round-trip via `event_to_json` / `event_from_json`.

---

### Message (history.py — SQLite)

Stored conversation record.

```
messages table
├── id          TEXT PRIMARY KEY        ULID
├── session_id  TEXT NOT NULL           Groups messages into conversations
├── role        TEXT NOT NULL           "user" | "assistant" | "tool"
├── content     TEXT NOT NULL           Raw text / tool result
├── tool_call_id TEXT                   Links tool result to call
├── tool_name   TEXT                    For tool role messages
└── created_at  INTEGER NOT NULL        Unix ms — ordering

Indexes:
  INDEX idx_messages_session ON messages(session_id, created_at)
  — Primary read pattern: load all messages for a session, ordered
```

`ConversationHistory.init()` is idempotent (CREATE TABLE IF NOT EXISTS) — safe to call on existing DBs.

---

### MemoryFile (memory.py — filesystem)

Memory is just markdown files. The agent writes them via `write_file`; the harness reads them for system prompt injection.

```
data/memory/
├── {slug}.md       Free-form markdown — agent writes these via write_file tool
└── index.json      Optional: cached keyword index for search_memory performance
                    Rebuilt on dir mtime change (lazy invalidation)
```

`search_memory(query)` keyword-matches against `.md` file content, returns top-K ranked by term frequency. No vector store in E1. `run_command` is the escape hatch if the agent needs semantic search later.

---

### TurnContext (context.py)

Assembled once per turn. Not persisted.

```python
@dataclass
class TurnContext:
    system_prompt: str       # AGENT.md + memory index + skill list
    skills_dir: Path         # For list_skills tool resolution
    memory_dir: Path         # For search_memory + write_file
    bot_dir: Path            # Root of the bot "distro"
    config: dict             # Parsed config.yaml (or empty dict)
```

`build_system_prompt()` concatenates:
1. `AGENT.md` content (required)
2. Memory index: `### Available Memory\n{list of .md files}`
3. Skills index: `### Available Skills\n{list of SKILL.md directories}`

---

### ToolDef (provider.py)

Passed to the model on every turn.

```python
@dataclass
class ToolDef:
    name: str           # Exact name as called by the model
    description: str    # What it does
    parameters: dict    # JSON Schema for args
```

Five ToolDefs built in `loop.py`. No dynamic registration in E1.

---

### Provider Protocol (provider.py)

```
Provider Protocol (runtime_checkable)
├── name: str
├── supports_streaming: bool
└── stream(messages, tools, *, model, system, context) → AsyncIterator[ProviderEvent]

ProviderEvent (Union of 4 types)
├── TextDelta       text: str
├── ToolCall        id, name, args: dict
├── ProviderDone    usage: ProviderUsage
└── ProviderError   message: str
```

Structural subtyping — `GeminiProvider` and `FakeProvider` both satisfy `isinstance(obj, Provider)` without inheriting anything. This is the key testability guarantee.

---

## Architecture Decision Records

### ADR-001: Dataclasses over Pydantic for AgentEvent

**Status:** Accepted  
**Context:** Events are emitted thousands of times per session during streaming. Need JSON serialization with zero runtime overhead. Pydantic adds ~25-30ms import time and `model_dump()` is 3-5x slower than `dataclasses.asdict()` for simple flat objects.  
**Decision:** Use `@dataclass` for all AgentEvent types and ProviderEvent types. Use Pydantic only for config loading and data validation at boundaries (not in streaming hot path).  
**Consequences:** No Pydantic schema export for events. Acceptable because the event protocol is internal to the framework.

---

### ADR-002: SQLite over PostgreSQL for history

**Status:** Accepted  
**Context:** Must run with zero infrastructure locally and on any cloud. The framework is cloud-agnostic — the Docker image must run identically on GCP Cloud Run, AWS ECS/Fargate, Azure Container Apps, or plain Docker Compose. No managed DB dependency.  
**Decision:** `aiosqlite` for async SQLite. DB path is fully configurable via `DB_URL` env var (defaults to `sqlite:///data/monkeybot.db`). The `/data` directory is a mount point — backed by whatever the operator provides: a local bind mount, a cloud volume (EFS, GCS FUSE, Azure File Share), or a tmpfs for ephemeral bots.  
**Consequences:** No multi-instance write concurrency to a single DB file. Acceptable — each bot instance owns its own SQLite file. Operators who need HA/multi-instance can mount a shared network volume or switch `DB_URL` to a PostgreSQL DSN (SQLAlchemy-compatible — no code change required).  
**Testing:** GCP (Cloud Run + GCS FUSE mount) is used as the validation target, but the implementation must not import or reference any GCP SDK.

---

### ADR-003: Five tools, no dynamic registration

**Status:** Accepted  
**Context:** The plan specifies exactly 5 tools: `run_command`, `read_file`, `write_file`, `search_memory`, `list_skills`. `run_command` is the escape hatch for everything else.  
**Decision:** Tools are hard-coded in the loop as a dict `{name: callable}`. No plugin system, no tool registration API. Adding a new tool requires modifying `loop.py` — which is intentional (it keeps the loop's surface area explicit).  
**Consequences:** Cannot add tools without a framework change. By design — this keeps the harness thin. Skills extend capability without touching the harness.

---

### ADR-004: Structural subtyping for Provider Protocol

**Status:** Accepted  
**Context:** Need to swap providers (Gemini → Claude → Fake) by changing one env var. Don't want to force all providers to inherit from a base class (would require importing the base class from core).  
**Decision:** `@runtime_checkable Protocol` — structural subtyping. `isinstance(provider, Provider)` works without inheritance.  
**Consequences:** Protocol drift possible if new methods are added. Mitigated by the strict constraint: Provider has exactly ONE method (`stream()`).

---

## Open Items

1. **google-genai >= 0.8 tool call streaming:** Verify the async streaming API correctly surfaces `ToolCall` events when the model requests a tool. This is a Day 2 validation item before implementing `GeminiProvider.stream()`.
2. **ulid-py API:** Confirm `ulid.new()` returns a string (not bytes) on v1.1+. Check in `uv.lock`.
3. **`context.py` dependency on `provider.py`:** The plan shows `provider.py` imports from `context.py` for `TurnContext`. Need to confirm import direction to avoid circular imports. Resolution: `TurnContext` goes in `context.py`; `provider.py` imports from `context.py`. Loop imports both. Clean DAG.

---

## Build Order (within E1)

Based on the dependency DAG — each item is independently testable:

```
Step  File(s)                            Testable Gate
────  ─────────────────────────────────  ──────────────────────────────────────
 1    pyproject.toml ✓ (exists)          uv sync installs cleanly
      .env.example, scripts/             scripts/bootstrap runs
 2    src/monkeybot/__init__.py          import monkeybot  (empty is fine)
 3    core/events.py                     All 10 event types round-trip JSON
 4    core/context.py                    build_system_prompt() with stub dirs
 5    core/provider.py                   GeminiProvider passes isinstance check
 6    core/inspector.py                  CommandTierInspector.inspect() returns Decision
 7    core/history.py                    save + load 3 messages, survives restart
 8    core/memory.py                     search_memory returns ranked results
 9    tools/run_command.py               echo "hi" returns "hi"; timeout → 124
      tools/file_ops.py                  read/write roundtrip
      tools/memory_ops.py                search_memory via tool interface
      tools/skill_ops.py                 list_skills returns dir names
10    providers/gemini.py                Real GEMINI_API_KEY → TextDelta + ProviderDone
11    core/loop.py                       FakeProvider → correct event sequence
12    gateway/cli.py                     stdin → loop.run() → stdout deltas
13    cli.py                             monkeybot run starts; exit terminates
14    tests/test_cold_start.py           import < 200ms; CLI startup < 500ms
      tests/unit/test_loop.py            test_simple_text_response passes
15    bots/example-bot/AGENT.md          monkeybot run --bot-dir ./bots/example-bot works
```

---

## Next Steps

- **Phase 1B:** Define full API contracts — `AgentLoop.run()` signature, `Provider.stream()` contract, `ConversationHistory` public API, tool function signatures, `CLIGateway` interface
- **Phase 1C:** Security (command injection defense in `run_command`), performance (import budget per module, async patterns), deployment (Docker, Cloud Run), observability (structured logging)
