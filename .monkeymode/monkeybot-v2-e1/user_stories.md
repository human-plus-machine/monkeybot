# User Stories — MonkeyBot v2 E1: Core Harness & Walking Skeleton

**Date:** 2026-05-13  
**Epic:** E1 — Core Harness & Walking Skeleton  
**PRT Source:** `.prt/monkeybot-v2/epics/e1-core-harness.md`  
**Design:** `.monkeymode/monkeybot-v2-e1/design/`

---

## Parallelization Plan

The E1 import DAG determines the batches. Stories must touch completely different files with zero overlap.

```
BATCH 1 — All start Day 1, fully parallel (4 stories)
─────────────────────────────────────────────────────
Story 1: Core Types & Protocols     core/events.py, core/context.py,
                                    core/provider.py, core/inspector.py

Story 2: Persistence Layer          core/history.py, core/memory.py

Story 3: Tools Layer                tools/run_command.py, tools/file_ops.py,
                                    tools/memory_ops.py, tools/skill_ops.py

Story 4: Infrastructure & Bot       scripts/*, .env.example,
                                    bots/example-bot/*, src/monkeybot/__init__.py

BATCH 2 — After Batch 1 complete (2 stories, parallel)
────────────────────────────────────────────────────────
Story 5: Gemini Provider            providers/gemini.py

Story 6: Agent Loop, Gateway & CLI  core/loop.py, gateway/cli.py,
                                    src/monkeybot/cli.py,
                                    tests/unit/test_loop.py,
                                    tests/test_cold_start.py
```

**File conflict check:**
- Batch 1: zero file overlaps between all 4 stories ✅
- Batch 2: zero file overlaps between Stories 5 and 6 ✅
- Batch 2 depends on Batch 1: yes — loop imports core types and persistence ✅ (by design)

---

## Story 1: Core Types & Protocols

**Type:** Feature  
**Priority:** Must (blocks everything else)  
**Batch:** 1 (starts Day 1)  
**Size:** S (1–2 days)  
**Dependencies:** NONE — pure Python stdlib  

### Description
As a bot developer,  
I want all 10 `AgentEvent` types, the `Provider` Protocol, `TurnContext`, and `ToolInspector` Protocol defined in stable, typed modules,  
So that every other story can implement against these contracts without waiting on each other.

### Technical Context

- **Files to create:**
  - `src/monkeybot/core/events.py`
  - `src/monkeybot/core/context.py`
  - `src/monkeybot/core/provider.py`
  - `src/monkeybot/core/inspector.py`
  - `tests/unit/test_events.py`
  - `tests/unit/test_context.py`
- **Files to modify:** `src/monkeybot/core/__init__.py` (remain empty — no top-level re-exports)
- **External deps:** stdlib only (`dataclasses`, `typing`, `pathlib`, `json`, `time`)
- **Design references:** `1a-discovery.md` "Core Data Model", `1b-contracts.md` "core/events.py" and "core/provider.py" sections

### Integration Contracts

**Interfaces defined by this story — used as mocks by all other stories:**

```python
# core/events.py
AgentEvent = Union[UserMessage, AssistantDelta, ToolCallStarted, ToolCallResult,
                   ApprovalRequest, ApprovalResponse, SubagentStarted, SubagentCompleted,
                   TurnComplete, ErrorEvent]

def event_to_json(event: AgentEvent) -> str: ...
def event_from_json(line: str) -> AgentEvent: ...  # raises ValueError on unknown kind

# core/context.py
@dataclass(frozen=True)
class TurnContext:
    agent_md: str
    memory_index: list[str]
    skills: list[SkillRef]
    user_id: str | None = None
    parent_run_id: str | None = None
    run_id: str | None = None
    def build_system_prompt(self) -> str: ...

def load_turn_context(agent_md_path: str, memory_path: str, skills_path: str, ...) -> TurnContext: ...

# core/provider.py
ProviderEvent = TextDelta | ToolCall | ProviderDone

@runtime_checkable
class Provider(Protocol):
    name: str
    supports_streaming: bool
    async def stream(self, messages, tools, *, model, system, context) -> AsyncIterator[ProviderEvent]: ...

# core/inspector.py
@dataclass
class Decision:
    kind: Literal["allow", "deny", "approve"]
    message: str | None = None

@runtime_checkable
class ToolInspector(Protocol):
    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision: ...

class CommandTierInspector:
    def __init__(self, config: dict): ...
    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision: ...

class RulesInspector:
    def __init__(self, denied_patterns: list[str]): ...
    async def check(self, call: ToolCall, ctx: TurnContext) -> Decision: ...
```

### Acceptance Criteria

- [ ] **Given** any of the 10 AgentEvent instances, **When** `event_to_json(e)` then `event_from_json(result)`, **Then** result equals the original event (round-trip equality)
- [ ] **Given** a JSON string with `kind: "unknown_kind"`, **When** `event_from_json(s)`, **Then** raises `ValueError`
- [ ] **Given** a valid `AGENT.md` + memory dir with 2 `.md` files + skills dir with 1 `SKILL.md`, **When** `load_turn_context(...)`, **Then** `TurnContext.memory_index` has 2 entries and `TurnContext.skills` has 1 entry
- [ ] **Given** missing `agent_md_path`, **When** `load_turn_context(...)`, **Then** raises `FileNotFoundError`
- [ ] **Given** missing `memory_path` or `skills_path`, **When** `load_turn_context(...)`, **Then** returns empty lists (not an error)
- [ ] **Given** `GeminiProvider()` instance (or any class with `name`, `supports_streaming`, `stream()`), **When** `isinstance(obj, Provider)`, **Then** returns `True` without inheritance
- [ ] **Given** `CommandTierInspector(config)` with `denied=["rm_all"]`, **When** `check(ToolCall(name="rm_all", ...))`, **Then** returns `Decision(kind="deny")`
- [ ] **Given** `CommandTierInspector(config)` with `requires_approval=["deploy"]`, **When** `check(ToolCall(name="deploy", ...))`, **Then** returns `Decision(kind="approve")`
- [ ] **Given** tool call not in any tier, **When** `CommandTierInspector.check(...)`, **Then** returns `Decision(kind="allow")`
- [ ] **Given** `RulesInspector(denied_patterns=["sudo"])`, **When** `check(ToolCall(args={"command": "sudo rm -rf"}))`, **Then** returns `Decision(kind="deny")`
- [ ] `build_system_prompt()` returns string containing AGENT.md content, a `## Memory Index` section, and a `## Available Skills` section when all three are populated
- [ ] `ruff check` and `mypy --strict` pass on all 4 files

### Out of Scope
- Any I/O (no file reads in events.py — that's context.py's job)
- Provider implementations (Story 5)
- History / memory implementations (Story 2)
- The loop (Story 6)

### Notes
- `provider.py` imports `TurnContext` from `context.py` — this is the only intra-story import dependency. Implement `context.py` first within this story.
- All 4 files are pure stdlib. Target: `import monkeybot.core.events` completes in < 5ms.
- `@dataclass(frozen=True)` on `TurnContext` — it must be safe to share across coroutines.

---

## Story 2: Persistence Layer

**Type:** Feature  
**Priority:** Must  
**Batch:** 1 (starts Day 1)  
**Size:** S (1–2 days)  
**Dependencies:** NONE — aiosqlite + pathlib only  

### Description
As a bot operator,  
I want conversation history to persist across process restarts via SQLite and memory files to be saved and searchable on disk,  
So that users don't lose context if a container restarts and the agent can recall past information.

### Technical Context

- **Files to create:**
  - `src/monkeybot/core/history.py`
  - `src/monkeybot/core/memory.py`
  - `tests/unit/test_history.py`
  - `tests/unit/test_memory.py`
- **Files to modify:** none
- **External deps:** `aiosqlite>=0.20`, `ulid-py>=1.1`
- **Design references:** `1a-discovery.md` "Message (SQLite)", `1b-contracts.md` "core/history.py" and "core/memory.py", `1c-operations.md` "SQLite WAL Mode"

### Integration Contracts

```python
# core/history.py
class ConversationHistory:
    def __init__(self, db_url: str = "sqlite:///data/monkeybot.db") -> None: ...
    async def init(self) -> None: ...       # idempotent, enables WAL mode
    async def save(self, session_id: str, role: str, content: str,
                   tool_call_id: str | None = None,
                   tool_name: str | None = None) -> None: ...
    async def load(self, session_id: str) -> list[Message]: ...
    async def clear(self, session_id: str) -> None: ...

# core/memory.py
def save_memory(memory_path: str, filename: str, content: str) -> str: ...
def search_memory(query: str, memory_path: str, max_results: int = 5) -> str: ...
```

**Note:** `Message` is imported from `core/provider.py` (Story 1). In isolation (before Story 1 is done), use this stub for testing:

```python
# Stub for parallel development — replace with Story 1's Message when available
from dataclasses import dataclass

@dataclass
class Message:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
```

When Story 1 completes, `Message` is imported from `core.provider` — the stub is replaced.

### Acceptance Criteria

- [ ] **Given** 3 messages saved to a session, **When** `history.load(session_id)`, **Then** returns all 3 in ascending `created_at` order
- [ ] **Given** `history.init()` called on an existing DB, **Then** does not fail and data is preserved (idempotent)
- [ ] **Given** process restart (new `ConversationHistory` instance pointing at same `db_url`), **When** `await history.init()` then `history.load(session_id)`, **Then** returns previously saved messages
- [ ] **Given** unknown `session_id`, **When** `history.load(session_id)`, **Then** returns `[]` (not an error)
- [ ] **Given** `history.init()` called with a `db_url` pointing to a non-existent directory, **Then** creates the directory and DB file automatically
- [ ] **Given** `search_memory("python async", memory_path)` with 5 files where 3 mention "async", **When** called, **Then** returns those 3 files ordered by match count, highest first
- [ ] **Given** `search_memory("xyz_nonexistent", memory_path)`, **Then** returns `"No memory files matched: xyz_nonexistent"`
- [ ] **Given** `memory_path` that doesn't exist, **When** `search_memory(...)`, **Then** returns `"No memory files found."` (not an error)
- [ ] **Given** `save_memory(memory_path, "my-note", "content")`, **Then** creates `{memory_path}/my-note.md` with the content
- [ ] WAL mode enabled: `PRAGMA journal_mode` returns `"wal"` after `init()`
- [ ] All tests use `tmp_path` (pytest fixture) — no global state

### Out of Scope
- History truncation / summarization (E3)
- Full-text search or embeddings for memory (escape hatch via `run_command`)
- PostgreSQL support (DB_URL DSN just needs to be stored; aiosqlite is the E1 impl)

### Notes
- DB file + parent directories created automatically in `init()`.
- `ulid.new()` call: confirm it returns a `str`. If it returns bytes, call `.str` on it.
- WAL mode + `synchronous=NORMAL` set in `init()` — see `1c-operations.md`.

---

## Story 3: Tools Layer

**Type:** Feature  
**Priority:** Must  
**Batch:** 1 (starts Day 1)  
**Size:** S (1 day)  
**Dependencies:** NONE — stdlib + pathlib only  

### Description
As an agent,  
I want five deterministic tools (run_command, read_file, write_file, search_memory, list_skills) that I can call to interact with the system,  
So that I can execute code, read files, save memory, and discover skills without needing new framework capabilities.

### Technical Context

- **Files to create:**
  - `src/monkeybot/tools/run_command.py`
  - `src/monkeybot/tools/file_ops.py`
  - `src/monkeybot/tools/memory_ops.py`
  - `src/monkeybot/tools/skill_ops.py`
  - `tests/unit/test_tools.py`
- **Files to modify:** `src/monkeybot/tools/__init__.py` (remain empty)
- **External deps:** stdlib only (`asyncio`, `os`, `pathlib`, `re`)
- **Design references:** `1b-contracts.md` "tools/ — Five Tool Functions", `1c-operations.md` "Security — read_file/write_file path traversal"

### Integration Contracts

```python
# tools/run_command.py
@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int

TOOL_DEF: dict   # JSON Schema tool definition for the model

async def run_command(
    command: str,
    working_dir: str | None = None,
    timeout: int = 30,
) -> CommandResult: ...

def format_result(r: CommandResult) -> str:
    """Format CommandResult as string for model consumption."""

# tools/file_ops.py
TOOL_DEFS: list[dict]   # Two entries: read_file + write_file

def read_file(path: str, *, allowed_roots: list[Path] | None = None) -> str: ...
def write_file(path: str, content: str, append: bool = False,
               *, allowed_roots: list[Path] | None = None) -> str: ...

# tools/memory_ops.py
TOOL_DEF: dict

def search_memory(query: str, memory_path: str, max_results: int = 5) -> str: ...
# Delegates to core/memory.py — use inline implementation for isolation

# tools/skill_ops.py
TOOL_DEF: dict

def list_skills(skills_path: str, filter: str | None = None) -> str: ...
```

Each tool module exports a `TOOL_DEF` (or `TOOL_DEFS`) dict — the JSON Schema definition passed to the model. The loop reads these at startup.

### Acceptance Criteria

- [ ] **Given** `run_command("echo hello")`, **Then** `CommandResult.stdout == "hello\n"` and `exit_code == 0`
- [ ] **Given** `run_command("sleep 100", timeout=1)`, **Then** `CommandResult.exit_code == 124` and `stderr` contains `"timed out"`
- [ ] **Given** `read_file` on existing file, **Then** returns full file content as string
- [ ] **Given** `read_file` on non-existent file, **Then** returns `"ERROR: File not found: {path}"`
- [ ] **Given** `read_file` with `allowed_roots=[tmp_path]` and path outside `tmp_path`, **Then** returns `"ERROR: Access denied: {path}"`
- [ ] **Given** `write_file(path, "hello")`, **Then** creates file with content; parent dirs created if missing
- [ ] **Given** `write_file(path, " world", append=True)` after existing file, **Then** file content is `"hello world"`
- [ ] **Given** `write_file` with `allowed_roots` and path outside roots, **Then** returns `"ERROR: Access denied: {path}"`
- [ ] **Given** 5 memory files where 3 match a query, **When** `search_memory(query, memory_path)`, **Then** returns 3 file excerpts ranked by score
- [ ] **Given** skills dir with 2 subdirs each containing `SKILL.md`, **When** `list_skills(skills_path)`, **Then** returns both skill names with descriptions
- [ ] **Given** `list_skills(skills_path, filter="web")` with only one skill matching "web", **Then** returns only that skill
- [ ] **Given** empty skills dir, **When** `list_skills(...)`, **Then** returns `"No skills found."`
- [ ] All tool `TOOL_DEF` dicts are valid JSON Schema with `"type": "object"`, `"properties"`, and `"required"` keys

### Out of Scope
- Inspector/safety checks (those live in the loop — Story 6 wires them)
- Tool registration / dynamic dispatch (the loop hard-codes the 5 tools)

### Notes
- `memory_ops.py` delegates to `core/memory.py`. Until Story 2 is merged, inline the 10-line `search_memory` function directly — the implementations are identical, so no merge conflict.
- `run_command` uses `asyncio.create_subprocess_shell` — it is the only natively async tool function. All others are sync.
- `format_result(r: CommandResult) -> str` converts to the model-readable string: `exit_code: N\nstdout: ...\nstderr: ...`

---

## Story 4: Project Infrastructure & Bot Template

**Type:** Feature  
**Priority:** Must  
**Batch:** 1 (starts Day 1)  
**Size:** XS (< 1 day)  
**Dependencies:** NONE — scripts and config files only  

### Description
As a bot developer,  
I want a working `scripts/bootstrap` script, a complete `.env.example`, a `__init__.py` with lazy exports, and a runnable `bots/example-bot/` template,  
So that I can clone the repo, run bootstrap, and have a documented starting point within minutes.

### Technical Context

- **Files to create:**
  - `scripts/bootstrap` (update existing with correct uv commands)
  - `scripts/run` (update existing)
  - `scripts/test` (update existing)
  - `.env.example` (create/update)
  - `bots/example-bot/AGENT.md`
  - `bots/example-bot/config.yaml`
  - `bots/example-bot/MEMORY.md`
  - `src/monkeybot/__init__.py` (lazy exports)
- **Files to modify:** none beyond the above
- **External deps:** none (bash scripts + markdown)
- **Design references:** `1a-discovery.md` "Environment & Tooling", `1b-contracts.md` "Public Module Surface", `1c-operations.md` "scripts/bootstrap Update" and "Docker"

### Integration Contracts

```python
# src/monkeybot/__init__.py — lazy exports to keep cold start fast
__version__ = "2.0.0"

def __getattr__(name: str):
    if name == "AgentLoop":
        from monkeybot.core.loop import AgentLoop
        return AgentLoop
    if name == "ConversationHistory":
        from monkeybot.core.history import ConversationHistory
        return ConversationHistory
    if name == "Provider":
        from monkeybot.core.provider import Provider
        return Provider
    raise AttributeError(f"module 'monkeybot' has no attribute {name!r}")
```

```bash
# scripts/bootstrap
#!/usr/bin/env bash
set -euo pipefail
echo "Installing dependencies..."
uv sync --extra gemini --extra dev
if [ ! -f .env ]; then
    cp .env.example .env
    echo "→ Edit .env and set GEMINI_API_KEY"
fi
mkdir -p data/memory
echo "Bootstrap complete. Run: scripts/run"
```

```bash
# scripts/run
#!/usr/bin/env bash
set -euo pipefail
source .env 2>/dev/null || true
exec python -m monkeybot run --bot-dir "${BOT_DIR:-./bots/example-bot}"
```

```bash
# scripts/test
#!/usr/bin/env bash
set -euo pipefail
exec python -m pytest "$@"
```

```bash
# .env.example
MODEL_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
DB_URL=sqlite:///data/monkeybot.db
MEMORY_PATH=./data/memory
SKILLS_PATH=./.agents/skills
AGENT_MD_PATH=./bots/example-bot/AGENT.md
LOG_LEVEL=INFO
BOT_DIR=./bots/example-bot
```

### Acceptance Criteria

- [ ] `scripts/bootstrap` runs successfully from a clean clone (no `.venv`): installs deps, creates `.env`, creates `data/memory/` dir
- [ ] `scripts/test` runs and exits 0 (even if no tests yet — just no import errors)
- [ ] `python -c "import monkeybot; print(monkeybot.__version__)"` prints `"2.0.0"` and completes in < 200ms
- [ ] `from monkeybot import AgentLoop` works after Story 6 is merged (lazy import, no error before)
- [ ] `.env.example` contains all 7 required vars with inline comments explaining each
- [ ] `bots/example-bot/AGENT.md` follows the AGENT.md template from `monkeybot_v2_plan.md` section "AGENT.md Template"
- [ ] `bots/example-bot/config.yaml` contains a `safety:` section with `denied_patterns` and `pre_approved` lists
- [ ] All 3 scripts are executable (`chmod +x`)

### Out of Scope
- Dockerfile (belongs in E1 Phase 6 integration — shipping the running image is integration)
- `docker-compose.yml` (same)
- CI workflow files (separate task, not E1 scope)

### Notes
- `__init__.py` uses `__getattr__` for lazy imports — do NOT add top-level `from monkeybot.core.loop import AgentLoop` style imports. That would pull in all dependencies at import time and blow the 200ms budget.
- All 3 scripts must be `chmod +x` and have `#!/usr/bin/env bash` shebangs.

---

## Story 5: Gemini Provider

**Type:** Feature  
**Priority:** Must  
**Batch:** 2 (after Batch 1 completes)  
**Size:** S (1–2 days)  
**Dependencies:** Story 1 (Provider Protocol, Message, ToolDef, TurnContext types)  

### Description
As a bot developer,  
I want a `GeminiProvider` that implements the `Provider` Protocol using the `google-genai` SDK,  
So that the agent can call Gemini models for LLM responses with a real API key while remaining swappable.

### Technical Context

- **Files to create:**
  - `src/monkeybot/providers/gemini.py`
  - `tests/integration/test_gemini_provider.py`
- **Files to modify:** `src/monkeybot/providers/__init__.py` (remain empty)
- **External deps:** `google-genai>=0.8` (optional extra — lazy imported inside `stream()`)
- **Design references:** `1b-contracts.md` "core/provider.py" and "Real Provider Integration Tests", `1c-operations.md` "Import Budget"

### Integration Contracts

```python
# providers/gemini.py
class GeminiProvider:
    """
    Implements Provider Protocol via structural subtyping.
    isinstance(GeminiProvider(), Provider) == True.
    """
    @property
    def name(self) -> str: return "gemini"

    @property
    def supports_streaming(self) -> bool: return True

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str = "gemini-2.0-flash",
        system: str = "",
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        # import google.generativeai as genai  ← LAZY IMPORT HERE (not at top of file)
        ...
```

**Open item from 1A to validate in this story:**
> Confirm `google-genai >= 0.8` async streaming API correctly surfaces `ToolCall` events when the model requests a function call in streaming mode. Document your finding in a comment in `gemini.py`.

### Acceptance Criteria

- [ ] `isinstance(GeminiProvider(), Provider)` returns `True`
- [ ] `import monkeybot.providers.gemini` completes in < 5ms (google-genai is NOT imported at module top level)
- [ ] **Given** valid `GEMINI_API_KEY` in env, **When** `stream([Message("user", "Say hello")], [], model="gemini-2.0-flash", system="You are helpful.")`, **Then** yields at least 1 `TextDelta` and exactly 1 `ProviderDone` with `input_tokens > 0`
- [ ] **Given** a tool definition and a prompt that triggers a tool call, **When** streaming, **Then** yields a `ToolCall` event with `call_id`, `name`, and `args` populated
- [ ] **Given** `GEMINI_API_KEY` not set, **When** `stream(...)` called, **Then** raises `KeyError` with message containing `"GEMINI_API_KEY"` (not a silent failure)
- [ ] `ProviderDone` is always the last yielded event (even on API error — yield `ProviderDone` with zero usage rather than propagating exception)
- [ ] `_convert_messages`: `role == "assistant"` maps to Gemini role `"model"`; `role == "user"` maps to `"user"` 
- [ ] Integration test is skipped (not failed) when `GEMINI_API_KEY` env var is not set: `@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="No API key")`
- [ ] `ruff check` and `mypy --strict` pass

### Out of Scope
- Claude or OpenAI providers (separate files, separate epics)
- Retry logic (E3)
- Cost estimation accuracy (best effort in E1 using model pricing constants)

### Notes
- **Critical lazy import rule:** `import google.generativeai as genai` must be inside `stream()`, not at module top. Violating this adds ~150ms to cold start and breaks CI.
- The `google-genai` SDK may use `google.generativeai` or `google.genai` depending on version — check `uv.lock` to confirm the installed package name before implementing.
- `ToolCall.call_id` must be a fresh ULID (use `import ulid; str(ulid.new())`).
- Cost estimation: hardcode a `PRICING` dict with per-million-token costs for common Gemini models; `0.0` is acceptable for unknown models.

---

## Story 6: Agent Loop, Gateway & CLI

**Type:** Feature  
**Priority:** Must  
**Batch:** 2 (after Batch 1 completes)  
**Size:** M (2–3 days)  
**Dependencies:** Stories 1–4 (all Batch 1 files must exist)  

### Description
As a bot developer,  
I want to run `python -m monkeybot run --bot-dir ./bots/example-bot` and immediately have a working interactive CLI agent that streams responses, dispatches tools, and persists history,  
So that I can test and iterate on bot behavior locally without any cloud setup.

### Technical Context

- **Files to create:**
  - `src/monkeybot/core/loop.py`
  - `src/monkeybot/gateway/cli.py`
  - `src/monkeybot/cli.py`
  - `tests/unit/test_loop.py`
  - `tests/test_cold_start.py`
- **Files to modify:** `src/monkeybot/gateway/__init__.py` (remain empty)
- **External deps:** `click>=8.0`, `asyncio` (stdlib), `logging` (stdlib)
- **Design references:** `1b-contracts.md` "core/loop.py", "gateway/cli.py", "cli.py", "Unit Testing — FakeProvider", all cold start test specs

### Integration Contracts

```python
# core/loop.py
class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        history: ConversationHistory,
        inspectors: list[ToolInspector],
        config: dict,   # keys: agent_md_path, memory_path, skills_path, model
    ) -> None: ...

    async def run(
        self,
        user_message: str,
        session_id: str,
        user_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """
        Yields in order: UserMessage → [AssistantDelta* | ToolCall cycle]* → TurnComplete
        TurnComplete is ALWAYS the last event.
        Never raises — all exceptions become ErrorEvent before TurnComplete.
        """

# gateway/cli.py
class CLIGateway:
    def __init__(self, loop: AgentLoop, session_id: str) -> None: ...
    async def run_interactive(self) -> None: ...

# src/monkeybot/cli.py
@click.group()
def main() -> None: ...

@main.command()
@click.option("--bot-dir", required=True, type=click.Path(exists=True))
@click.option("--session-id", default=None)
@click.option("--model", default=None)
def run(bot_dir: str, session_id: str | None, model: str | None) -> None: ...
```

**`FakeProvider` for tests (defined in `tests/unit/test_loop.py`):**

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

### Acceptance Criteria

**Loop — event stream contract:**
- [ ] **Given** `FakeProvider([TextDelta("Hello"), TextDelta(" world"), ProviderDone(usage=...)])`, **When** `loop.run("Hi", "session-1")`, **Then** yields `UserMessage`, `AssistantDelta("Hello")`, `AssistantDelta(" world")`, `TurnComplete` in order
- [ ] **Given** `FakeProvider([ToolCall(call_id="c1", name="read_file", args={"path": str(agent_md)}), ProviderDone(...)])` followed by `FakeProvider([TextDelta("Done"), ProviderDone(...)])`, **Then** yields `ToolCallStarted`, `ToolCallResult`, then `AssistantDelta("Done")`, `TurnComplete`
- [ ] **Given** inspector that denies the tool call, **When** tool call dispatched, **Then** `ToolCallResult` has `error` populated, loop continues (does not crash)
- [ ] **Given** `FakeProvider` that raises mid-stream, **Then** `ErrorEvent` is yielded before `TurnComplete`; `TurnComplete` is always last
- [ ] After `loop.run()` completes, `history.load(session_id)` returns at least 2 messages (user + assistant)

**Loop — tool dispatch:**
- [ ] All 5 known tool names dispatch correctly and return string results
- [ ] Unknown tool name returns `"Unknown tool: {name}"` as the `ToolCallResult.result`
- [ ] `run_command` passes `working_dir` from `config["bot_dir"]` (defaults to bot directory)

**Cold start:**
- [ ] `test_import_time`: `import monkeybot` completes in < 200ms (subprocess test)
- [ ] `test_cli_startup_time`: `python -m monkeybot --help` completes in < 500ms (subprocess test)

**CLI gateway:**
- [ ] `monkeybot run --bot-dir ./bots/example-bot` starts without error with valid `.env` and `AGENT.md`
- [ ] User messages receive streamed text responses printed inline
- [ ] Tool calls print `[Tool: name(args)]` to stdout
- [ ] `exit` (case-insensitive) or EOF terminates cleanly with exit code 0
- [ ] Structured JSON logging: each turn logs a `turn_complete` record with `session_id`, `run_id`, `input_tokens`, `duration_ms`

**Quality:**
- [ ] `ruff check src/` and `mypy --strict src/` both clean
- [ ] `tests/unit/test_loop.py::test_simple_text_response` passes with `FakeProvider`

### Out of Scope
- Google Chat gateway (E2)
- Subagent spawning (E4)
- HITL / approval flow (E2)
- Usage DB (E3)
- Max tool call depth enforcement (E2)

### Notes
- The loop re-enters `provider.stream()` after each tool call until the model sends no more tool calls in a single response. This is the agentic loop.
- `run_id` is a fresh ULID at the top of each `run()` call.
- Tool functions (except `run_command`) are sync — wrap in `await asyncio.to_thread(fn, **args)` in the loop.
- `run_command`'s `working_dir` defaults to `config.get("bot_dir")` — the bot's home directory, not the system root.
- The `_safe_env()` function (from `1c-operations.md`) must be used in `run_command` to redact API keys from the subprocess environment.
- The CLI wires `GeminiProvider` by default (selected via `MODEL_PROVIDER` env var). Use `importlib.import_module` or a simple `if/elif` — no dynamic plugin system.

---

## Parallelization Summary

```
Day 1 — Batch 1 (all 4 start simultaneously):
┌─────────────────────┐ ┌──────────────────┐ ┌──────────────┐ ┌────────────────┐
│ Story 1             │ │ Story 2          │ │ Story 3      │ │ Story 4        │
│ Core Types          │ │ Persistence      │ │ Tools        │ │ Infrastructure │
│ events.py           │ │ history.py       │ │ run_command  │ │ scripts/       │
│ context.py          │ │ memory.py        │ │ file_ops     │ │ .env.example   │
│ provider.py         │ │                  │ │ memory_ops   │ │ bots/example   │
│ inspector.py        │ │                  │ │ skill_ops    │ │ __init__.py    │
└─────────────────────┘ └──────────────────┘ └──────────────┘ └────────────────┘

Day 2–3 — Batch 2 (after Batch 1 merges):
┌───────────────────────────┐  ┌──────────────────────────────────┐
│ Story 5                   │  │ Story 6                          │
│ Gemini Provider           │  │ Agent Loop + Gateway + CLI       │
│ providers/gemini.py       │  │ core/loop.py                     │
│ integration test          │  │ gateway/cli.py                   │
│                           │  │ src/monkeybot/cli.py             │
│                           │  │ tests/unit/test_loop.py          │
│                           │  │ tests/test_cold_start.py         │
└───────────────────────────┘  └──────────────────────────────────┘

Phase 6 — Integration:
  Wire GeminiProvider into CLI → E2E smoke test → Milestone 1 ✅
  python -m monkeybot run --bot-dir ./bots/example-bot
```

No story blocks another within its batch. No merge conflicts (zero file overlap within each batch).
