# Design: MonkeyBot v2 E1 — Core Harness & Walking Skeleton
## Phase 1B: Detailed Contracts

**Date:** 2026-05-13  
**Status:** Phase 1B — Method Contracts, Component Integration, Testing Strategy  
**Version:** 1.0

---

## Overview

This document is adapted from the HTTP-API contract template to a Python framework context. "API contracts" are Python method signatures and async generator protocols; "integration points" are component wiring boundaries; "versioning" is the public module surface.

---

## Public API Contracts

### Common Patterns

#### Error Handling

All public functions use one of three error strategies:

| Strategy | When | Example |
|----------|------|---------|
| Return sentinel string | Tool functions (model reads the error) | `"ERROR: File not found: {path}"` |
| Raise `ValueError` | Contract violations at construction | `ConversationHistory.__init__` with bad DSN |
| Yield `ErrorEvent` | Async runtime failures in the loop | Provider stream failure mid-turn |

The loop NEVER raises into the gateway — all runtime errors become `ErrorEvent` in the stream.

#### Async Convention

All I/O is `async`. Sync helper functions (tool implementations, string manipulation) are plain `def`. The rule: if it touches the filesystem, network, or subprocess → `async`. Sync wrappers are forbidden.

#### `None` vs sentinel

- Functions that "can't find it" return a sentinel string (tools) or empty list (search results).
- Functions that "need it or it's a bug" raise `ValueError` at init time.
- No `Optional` return types for expected-to-succeed operations.

---

### `core/events.py` — AgentEvent Stream

```python
AgentEvent = Union[
    UserMessage, AssistantDelta, ToolCallStarted, ToolCallResult,
    ApprovalRequest, ApprovalResponse, SubagentStarted, SubagentCompleted,
    TurnComplete, ErrorEvent,
]

def event_to_json(event: AgentEvent) -> str:
    """Serialize any AgentEvent to a JSON string. Uses dataclasses.asdict()."""

def event_from_json(line: str) -> AgentEvent:
    """
    Deserialize a JSON string back to the correct AgentEvent subtype.
    Raises ValueError if `kind` is unknown.
    Round-trip guarantee: event_from_json(event_to_json(e)) == e for all event types.
    """
```

**Contract guarantees:**
- Every `AgentEvent` type has a `kind: Literal[...]` field — used as the discriminator.
- `event_to_json` / `event_from_json` are exact inverses: `event_from_json(event_to_json(e)) == e`.
- All fields are JSON-serializable primitives (str, int, float, bool, dict, None).
- `timestamp` fields are Unix milliseconds (int), not ISO strings.

**The 10 event types and their purpose:**

| Event | Emitted by | Purpose |
|-------|-----------|---------|
| `UserMessage` | Gateway → loop | Input envelope |
| `AssistantDelta` | Loop | Streaming text chunk |
| `ToolCallStarted` | Loop | Before tool dispatch |
| `ToolCallResult` | Loop | After tool returns |
| `ApprovalRequest` | Loop (E2) | HITL gate — loop pauses |
| `ApprovalResponse` | Gateway (E2) | HITL resume signal |
| `SubagentStarted` | Loop (E4) | Subagent spawn notification |
| `SubagentCompleted` | Loop (E4) | Subagent done notification |
| `TurnComplete` | Loop | End of turn + token accounting |
| `ErrorEvent` | Loop | Non-fatal runtime error |

---

### `core/context.py` — TurnContext

```python
@dataclass(frozen=True)
class SkillRef:
    name: str           # Directory name (e.g. "web-search")
    description: str    # First non-heading line from SKILL.md, max 120 chars
    path: str           # Absolute path to SKILL.md

@dataclass(frozen=True)
class TurnContext:
    agent_md: str                   # Full text of AGENT.md
    memory_index: list[str]         # One summary line per memory file: "{stem}: {first_line}"
    skills: list[SkillRef]          # All SKILL.md refs found in skills_path
    user_id: str | None = None
    parent_run_id: str | None = None
    run_id: str | None = None

    def build_system_prompt(self) -> str:
        """
        Assemble: AGENT.md + Memory Index section + Available Skills section.
        Returns a single string — the `system` param passed to Provider.stream().
        """

def load_turn_context(
    agent_md_path: str,
    memory_path: str,
    skills_path: str,
    user_id: str | None = None,
    parent_run_id: str | None = None,
    run_id: str | None = None,
) -> TurnContext:
    """
    Load TurnContext synchronously from disk.
    - Raises FileNotFoundError if agent_md_path does not exist.
    - Returns empty memory_index and skills if those paths don't exist (not an error).
    """
```

**Contract guarantees:**
- `TurnContext` is frozen (immutable) — safe to share across coroutines.
- `load_turn_context` is synchronous (reads small files; no async needed).
- `build_system_prompt()` is pure (no I/O, no side effects).
- Memory index format: `"{file_stem}: {first non-empty line of file}"` — one entry per `.md` file.
- Skills scan depth: one level only (`skills_path/*/SKILL.md`) — no recursive subdirectories.

---

### `core/provider.py` — Provider Protocol

```python
@dataclass
class Message:
    role: str               # "user" | "assistant" | "tool"
    content: str            # Text or tool result string
    tool_call_id: str | None = None
    tool_name: str | None = None

@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict        # JSON Schema object

# ProviderEvent types
@dataclass class TextDelta:       text: str
@dataclass class ToolCall:        call_id: str; name: str; args: dict
@dataclass class ProviderUsage:   input_tokens: int; output_tokens: int; cached_tokens: int = 0; cost_usd: float = 0.0
@dataclass class ProviderDone:    usage: ProviderUsage

ProviderEvent = TextDelta | ToolCall | ProviderDone

@runtime_checkable
class Provider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def supports_streaming(self) -> bool: ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str,
        system: str,
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]: ...
```

**Contract guarantees:**
- `Provider` is a `@runtime_checkable Protocol` — `isinstance(obj, Provider)` works without inheritance.
- `stream()` MUST yield exactly one `ProviderDone` as the final event.
- `stream()` MAY yield zero or more `TextDelta` events.
- `stream()` MAY yield zero or more `ToolCall` events.
- `ProviderDone.usage` MUST have non-zero `input_tokens` when real tokens were consumed.
- If the provider errors mid-stream, it yields `ProviderDone` with zero usage and raises nothing (errors are surfaced via the event, not exceptions leaking into the loop).
- `ToolCall.call_id` is a stable ULID — the loop uses it to correlate `ToolCallResult` back to the call.

---

### `core/inspector.py` — ToolInspector Protocol

```python
@dataclass
class Decision:
    kind: Literal["allow", "deny", "approve"]
    message: str | None = None  # Human-readable reason (for deny/approve)

@runtime_checkable
class ToolInspector(Protocol):
    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision: ...

class CommandTierInspector:
    """
    Reads tiers from YAML config:
      pre_approved: [list of tool names]
      requires_approval: [list of tool names]
      denied: [list of tool names]
    """
    def __init__(self, config: dict): ...
    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision: ...

class RulesInspector:
    """Blocks calls where args contain any denied pattern string."""
    def __init__(self, denied_patterns: list[str]): ...
    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision: ...
```

**Contract guarantees:**
- Inspectors are checked in order; first `deny` or `approve` wins; if all `allow` → call proceeds.
- `RulesInspector` pattern matching is substring match on `str(call.args)` — simple, no regex.
- In E1 (no safety config loaded), the default inspector list is empty → all calls allowed.

---

### `core/history.py` — ConversationHistory

```python
class ConversationHistory:
    def __init__(self, db_url: str = "sqlite:///data/monkeybot.db") -> None:
        """
        db_url: SQLite path as "sqlite:///path/to/file.db"
                or PostgreSQL DSN "postgresql+asyncpg://user:pass@host/db"
        Does NOT connect at init — call await init() first.
        """

    async def init(self) -> None:
        """
        Create tables if they don't exist. Idempotent — safe on existing DB.
        Must be called before save() or load().
        """

    async def save(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """
        Persist one message. role ∈ {"user", "assistant", "tool"}.
        Assigns a ULID as the message id.
        """

    async def load(self, session_id: str) -> list[Message]:
        """
        Load all messages for a session ordered by created_at ASC.
        Returns empty list if session_id not found (not an error).
        """

    async def clear(self, session_id: str) -> None:
        """Delete all messages for a session. Used in tests."""
```

**SQLite schema:**

```sql
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT    PRIMARY KEY,        -- ULID
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,           -- "user" | "assistant" | "tool"
    content     TEXT    NOT NULL,
    tool_call_id TEXT,
    tool_name   TEXT,
    created_at  INTEGER NOT NULL            -- Unix milliseconds
);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at);
```

**Contract guarantees:**
- `init()` is idempotent — `CREATE TABLE IF NOT EXISTS`.
- `load()` returns messages ordered oldest-first (ascending `created_at`).
- Schema survives process restart — data persists to the `db_url` path.
- DB file and parent directories are created automatically if they don't exist.

---

### `core/memory.py` — Memory Operations

```python
def save_memory(memory_path: str, filename: str, content: str) -> str:
    """
    Write a markdown file to memory_path/{filename}.md.
    Creates parent directories if needed.
    Returns: "OK: saved memory/{filename}.md"
    """

def search_memory(query: str, memory_path: str, max_results: int = 5) -> str:
    """
    Keyword search over *.md files in memory_path.
    Scoring: count of query keywords found (case-insensitive).
    Returns: formatted string with up to max_results file excerpts (first 500 chars each).
    Returns: "No memory files found." if memory_path doesn't exist.
    Returns: "No memory files matched: {query}" if no files score > 0.
    """
```

**No index file in E1.** The memory directory is expected to stay small (< 50 files) during E1. A cached index can be added in a later epic if scan time becomes measurable.

---

### `tools/` — Five Tool Functions

All tool functions are synchronous (disk I/O only, no network). Called via `await asyncio.to_thread(fn, ...)` in the loop to stay non-blocking.

```python
# tools/run_command.py
async def run_command(
    command: str,
    working_dir: str | None = None,
    timeout: int = 30,
) -> CommandResult:
    """
    Execute shell command via asyncio.create_subprocess_shell.
    On timeout: returns CommandResult(exit_code=124, stderr="Command timed out after {n}s").
    Inherits os.environ — no credential injection needed.
    """

# tools/file_ops.py
def read_file(path: str) -> str:
    """Returns file content, or 'ERROR: File not found: {path}' if missing."""

def write_file(path: str, content: str, append: bool = False) -> str:
    """Creates parent dirs. Returns 'OK: wrote {n} chars to {path}'."""

# tools/memory_ops.py
def search_memory(query: str, memory_path: str, max_results: int = 5) -> str:
    """Delegates to core/memory.py search_memory."""

# tools/skill_ops.py
def list_skills(skills_path: str, filter: str | None = None) -> str:
    """Returns formatted skill list, or 'No skills found.' if none match."""
```

**Tool dispatch contract in the loop:**

```
tool_name → callable → result_str
─────────────────────────────────
"run_command"    → run_command(**args)        async
"read_file"      → read_file(**args)          sync → asyncio.to_thread
"write_file"     → write_file(**args)         sync → asyncio.to_thread
"search_memory"  → search_memory(**args)      sync → asyncio.to_thread
"list_skills"    → list_skills(**args)        sync → asyncio.to_thread
unknown name     → "Unknown tool: {name}"    immediate
```

All tool results are coerced to `str` before being fed back to the model. A `CommandResult` is formatted as:
```
exit_code: {n}
stdout: {stdout}
stderr: {stderr}  (omitted if empty)
duration_ms: {n}
```

---

### `core/loop.py` — AgentLoop

```python
class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        history: ConversationHistory,
        inspectors: list[ToolInspector],
        config: dict,
    ) -> None:
        """
        config keys (all required):
          agent_md_path: str
          memory_path: str
          skills_path: str
          model: str          — passed as-is to Provider.stream()
        """

    async def run(
        self,
        user_message: str,
        session_id: str,
        user_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """
        Full turn execution. Yields events in this guaranteed order:
          1. UserMessage
          2. [AssistantDelta* | ToolCallStarted, ToolCallResult]* (zero or more cycles)
          3. TurnComplete  — always the final event, even on error
          (ErrorEvent may be yielded before TurnComplete if a non-fatal error occurs)

        Never raises — all exceptions are caught and emitted as ErrorEvent.
        Saves user message + assistant response to history on TurnComplete.
        """
```

**Turn execution flow:**

```
run(user_message, session_id)
  → yield UserMessage
  → load history (await history.load(session_id))
  → load_turn_context(...)
  → save user message (await history.save(session_id, "user", user_message))
  → loop:
      provider.stream(messages, tools, model=..., system=ctx.build_system_prompt())
      for event in stream:
          TextDelta      → yield AssistantDelta(text=event.text)
          ToolCall       → run inspector chain
                           if deny:  yield ToolCallResult(error="Denied: {msg}")
                           if allow: yield ToolCallStarted
                                     dispatch_tool(call)
                                     yield ToolCallResult
                                     append tool result to messages, continue loop
          ProviderDone   → break inner loop
      if no ToolCall in last iteration → break outer loop
  → save assistant response (await history.save(session_id, "assistant", full_text))
  → yield TurnComplete(run_id, input_tokens, output_tokens, cost_usd, duration_ms)
```

**Contracts:**
- `run()` is an `AsyncIterator[AgentEvent]` — callers use `async for event in loop.run(...)`.
- `TurnComplete` is ALWAYS the last event, even if an `ErrorEvent` was yielded earlier.
- The loop re-enters `provider.stream()` after each tool call until the model responds with no tool calls.
- Maximum tool call depth: no hard limit in E1 (model's context window is the natural limit). Hard limit added in E2 safety config.
- `run_id` in `TurnComplete` is a fresh ULID generated at the start of each call to `run()`.

---

### `gateway/cli.py` — CLIGateway

```python
class CLIGateway:
    def __init__(self, loop: AgentLoop, session_id: str) -> None: ...

    async def run_interactive(self) -> None:
        """
        Read-eval-print loop:
          - Print prompt "> "
          - Read line from stdin (asyncio-compatible)
          - If line == "exit" or EOF → break cleanly
          - Call loop.run(line, session_id)
          - For AssistantDelta: print text inline (no newline)
          - For ToolCallStarted: print "[Tool: {name}({args})]"
          - For ToolCallResult: print "[Result: {result[:100]}]"  (truncated)
          - For TurnComplete: print newline + token summary
          - For ErrorEvent: print "Error: {message}" to stderr
        """
```

**Contract guarantees:**
- `CLIGateway` only imports from `events.py` and `loop.py` — never from providers or tools directly.
- `exit` (exact string, case-insensitive) or EOF terminates the loop cleanly (no traceback).
- Streaming output is flushed immediately on each `AssistantDelta` (no buffering).

---

### `cli.py` — Entry Point

```python
# monkeybot run --bot-dir ./bots/example-bot [--session-id <id>] [--model gemini-2.0-flash]
@click.command()
@click.option("--bot-dir", required=True, type=click.Path(exists=True))
@click.option("--session-id", default=None)
@click.option("--model", default=None)
def run(bot_dir: str, session_id: str | None, model: str | None) -> None:
    """
    Wire up all components from environment + bot-dir, then hand off to CLIGateway.
    Provider selected by MODEL_PROVIDER env var (default: "gemini").
    session_id defaults to a fresh ULID if not provided.
    """
```

**Env vars consumed by `cli.py`:**

| Var | Default | Purpose |
|-----|---------|---------|
| `MODEL_PROVIDER` | `gemini` | Which Provider class to instantiate |
| `GEMINI_API_KEY` | — (required if gemini) | Passed to GeminiProvider |
| `DB_URL` | `sqlite:///data/monkeybot.db` | Passed to ConversationHistory |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Integration Points

### Component Wiring Diagram

```
cli.py
  │ instantiates
  ├── ConversationHistory(db_url)
  ├── GeminiProvider()
  ├── AgentLoop(provider, history, inspectors=[], config)
  └── CLIGateway(loop, session_id)
        │ calls
        └── loop.run(user_message, session_id)
              │ calls
              ├── history.load(session_id)          → list[Message]
              ├── load_turn_context(...)             → TurnContext
              ├── provider.stream(messages, tools)   → AsyncIterator[ProviderEvent]
              ├── inspector.check(call, ctx)         → Decision  (per tool call)
              ├── dispatch_tool(name, args)          → str
              └── history.save(session_id, ...)
```

### Import DAG (E1 — no circular dependencies)

```
events.py       ← no internal deps
context.py      ← no internal deps
provider.py     ← context.py (TurnContext type only)
inspector.py    ← provider.py (ToolCall), context.py (TurnContext)
history.py      ← no internal deps (aiosqlite only)
memory.py       ← no internal deps (pathlib only)
tools/*.py      ← memory.py (memory_ops only), no other core deps
providers/gemini.py ← core/provider.py, core/context.py
loop.py         ← ALL of the above
gateway/cli.py  ← loop.py, events.py
cli.py          ← loop.py, gateway/cli.py, history.py, providers/
```

No file imports from `loop.py` except `gateway/` and `cli.py`. This prevents circular imports and keeps the harness self-contained.

### External Integrations (E1)

| Integration | Direction | Failure Handling |
|-------------|-----------|-----------------|
| Google Gemini API | Outbound (via google-genai SDK) | `ProviderDone` with zero usage + `ErrorEvent` |
| SQLite file | Local read/write | `ErrorEvent` if DB not writable; history returns `[]` on read failure |
| Filesystem (memory, skills, AGENT.md) | Local read/write | Sentinel strings returned; missing `agent_md_path` raises `FileNotFoundError` at startup |
| `stdin` / `stdout` | Local I/O | EOF → clean exit |

### Events Produced by the Loop (E1)

All 10 `AgentEvent` types are defined in E1, but only these are emitted in the walking skeleton:

| Event | Emitted in E1? | Notes |
|-------|---------------|-------|
| `UserMessage` | ✅ | Every turn |
| `AssistantDelta` | ✅ | Every streaming chunk |
| `ToolCallStarted` | ✅ | Before each tool dispatch |
| `ToolCallResult` | ✅ | After each tool returns |
| `ApprovalRequest` | ❌ | E2 — HITL not wired yet |
| `ApprovalResponse` | ❌ | E2 |
| `SubagentStarted` | ❌ | E4 |
| `SubagentCompleted` | ❌ | E4 |
| `TurnComplete` | ✅ | End of every turn |
| `ErrorEvent` | ✅ | Non-fatal runtime errors |

The event types for future epics are defined now so gateways can handle all 10 types from day one, even if the loop doesn't emit them yet.

---

## Testing Strategy

### Unit Testing

**Coverage target:** 100% of public contract paths; 80% line coverage overall.

**Mock boundaries — what to fake:**

| Component | How to fake |
|-----------|------------|
| Provider | `FakeProvider` (scripted event list) — no mocking framework needed |
| SQLite | `tmp_path` fixture + fresh DB per test |
| Filesystem | `tmp_path` fixture — real files in temp directory |
| Time | `time.monotonic` patch (pytest monkeypatch) |
| ULID | Not mocked — ULIDs are opaque IDs, exact value irrelevant |
| Env vars | `monkeypatch.setenv` per test |

**`FakeProvider` pattern (the key testing primitive):**

```python
class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, events: list[ProviderEvent]) -> None:
        self.events = events

    async def stream(self, messages, tools, *, model, system, context=None):
        async def _gen():
            for event in self.events:
                yield event
        return _gen()
```

**Unit test coverage requirements per module:**

| Module | Required test cases |
|--------|-------------------|
| `events.py` | All 10 event types round-trip `event_to_json`/`event_from_json` |
| `context.py` | `build_system_prompt()` with/without memory, with/without skills |
| `context.py` | `load_turn_context()` — missing memory_path OK, missing agent_md raises |
| `history.py` | Save 3 messages → load returns 3 ordered; survives `init()` on existing DB |
| `memory.py` | `search_memory` — 3 matching out of 5 files returned ranked |
| `memory.py` | `search_memory` — no matches returns sentinel string |
| `tools/run_command` | `echo "hi"` returns stdout; timeout returns exit_code=124 |
| `tools/file_ops` | Read existing, read missing, write new, write + append |
| `tools/skill_ops` | Lists skills, filter works, empty dir returns sentinel |
| `inspector.py` | `CommandTierInspector` — allow, deny, approve decisions |
| `loop.py` | Simple text response (FakeProvider with 2 TextDelta + ProviderDone) |
| `loop.py` | One tool call cycle (FakeProvider with ToolCall → re-entry → TextDelta + ProviderDone) |
| `loop.py` | Denied tool call → ToolCallResult with error, loop continues |
| `loop.py` | TurnComplete always final event, even after ErrorEvent |

### Integration Testing

**Scope:** Full turn with real SQLite, real filesystem, `FakeProvider`.

**Test setup pattern:**
```python
@pytest.fixture
async def loop_with_real_storage(tmp_path):
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# Test\nYou are a test agent.")
    (tmp_path / "memory").mkdir()
    (tmp_path / "skills").mkdir()
    db_path = str(tmp_path / "test.db")

    history = ConversationHistory(db_url=f"sqlite:///{db_path}")
    await history.init()

    provider = FakeProvider([
        TextDelta(text="Hello!"),
        ProviderDone(usage=ProviderUsage(input_tokens=10, output_tokens=3)),
    ])

    return AgentLoop(provider=provider, history=history, inspectors=[], config={
        "agent_md_path": str(agent_md),
        "memory_path": str(tmp_path / "memory"),
        "skills_path": str(tmp_path / "skills"),
        "model": "fake",
    })
```

**Key integration scenarios:**

| Scenario | What it tests |
|----------|--------------|
| 3-turn conversation | History accumulates; `load(session_id)` returns all 3 exchanges |
| Process restart simulation | Write 3 messages to DB, create new `ConversationHistory`, `init()`, `load()` — all 3 present |
| Tool call writes a memory file | `write_file` tool creates file in `memory_path`; `search_memory` finds it |
| Multi-turn with tool calls | Messages include tool role entries in correct order |

### CI-Gated Cold Start Tests

```python
# tests/test_cold_start.py — run on every PR

def test_import_time():
    """import monkeybot must complete in < 200ms."""
    start = time.monotonic()
    result = subprocess.run(["python", "-c", "import monkeybot"], capture_output=True, timeout=10)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0, result.stderr.decode()
    assert elapsed_ms < 200, f"Import took {elapsed_ms:.0f}ms (limit: 200ms)"

def test_cli_startup_time():
    """monkeybot --help must start in < 500ms."""
    start = time.monotonic()
    result = subprocess.run(["python", "-m", "monkeybot", "--help"], capture_output=True, timeout=10)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.returncode == 0
    assert elapsed_ms < 500, f"CLI startup took {elapsed_ms:.0f}ms (limit: 500ms)"
```

**Import budget per module** (enforced by the cold start test):

| Module | Budget | Why |
|--------|--------|-----|
| `events.py` | < 5ms | stdlib dataclasses only |
| `context.py` | < 5ms | pathlib only |
| `history.py` | < 10ms | aiosqlite import |
| `tools/*.py` | < 5ms each | No heavy imports |
| `providers/gemini.py` | < 5ms | google-genai is **lazy imported** inside `stream()` |
| `loop.py` | < 20ms | All of the above |
| `cli.py` | < 30ms | click + all of the above |

The critical rule: **`google-genai`, `anthropic`, and any LLM SDK must be lazy-imported** (inside the method that uses them, not at module top level).

### Real Provider Integration Tests (CI optional, local required)

```python
# tests/integration/test_gemini_provider.py
# Skipped if GEMINI_API_KEY not set

@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="No API key")
async def test_gemini_real_response(tmp_path):
    """Confirms the real Gemini API returns at least 1 TextDelta + ProviderDone with tokens."""
    provider = GeminiProvider()
    events = []
    async for event in await provider.stream(
        messages=[Message(role="user", content="Say 'hello' and nothing else.")],
        tools=[],
        model="gemini-2.0-flash",
        system="You are a minimal test assistant.",
    ):
        events.append(event)

    text_events = [e for e in events if isinstance(e, TextDelta)]
    done_events = [e for e in events if isinstance(e, ProviderDone)]

    assert len(text_events) >= 1
    assert len(done_events) == 1
    assert done_events[0].usage.input_tokens > 0
```

### Linting & Type Checking (CI-gated)

```bash
ruff check src/         # zero warnings
mypy --strict src/      # zero errors
```

All public functions require type annotations. No `Any` in public signatures. `# type: ignore` is permitted only with an explanatory comment.

---

## Public Module Surface (Versioning)

MonkeyBot v2 is a framework — the "API version" is the Python import surface.

**Stable public exports (must not change without migration path):**

```python
# src/monkeybot/__init__.py — the public API
from monkeybot.core.events import AgentEvent, event_to_json, event_from_json
from monkeybot.core.loop import AgentLoop
from monkeybot.core.history import ConversationHistory
from monkeybot.core.provider import Provider, Message, ToolDef
from monkeybot.core.context import TurnContext, load_turn_context
```

**Internal — no stability guarantee:**
- `monkeybot.core._*` (anything prefixed with `_`)
- `monkeybot.providers.*` (implementation details)
- `monkeybot.gateway.*` (deployment-specific)

**Breaking change policy:**
- Bump `version` in `pyproject.toml` for any change to the stable surface.
- `CHANGELOG.md` entry required for breaking changes.
- No breaking changes within E1 scope.

---

## Next Steps

- **Phase 1C:** Security (command injection in `run_command`, path traversal in `read_file`/`write_file`), performance (async patterns, import budget enforcement), Docker packaging, structured logging, Cloud Run deployment notes.
