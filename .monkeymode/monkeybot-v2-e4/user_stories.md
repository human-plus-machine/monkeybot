# User Stories — monkeybot-v2-e4
## E4: Subagents, Durability & LLM Council

**Phase:** 2 — User Stories  
**Date:** 2026-05-13  
**Source PRT:** `.prt/monkeybot-v2/epics/e4-subagents-council.md`

---

## Parallelization Plan

E4 has **four independent components** with near-zero file overlap:

| Story | Component | Key Files |
|---|---|---|
| Story 1 | Subagent Protocol & Scratch Dirs | `core/subagent_proto.py`, `core/runs.py`, `subagents/researcher.py`, `tests/unit/test_subagent_proto.py`, `tests/fixtures/echo_agent.py` |
| Story 2 | Subagent Registry | `core/subagent_registry.py`, `tests/unit/test_subagent_registry.py`, `bots/example-bot/config.yaml` (`subagents:` block only) |
| Story 3 | Durable Run Store | `core/durable_runs.py`, `tests/unit/test_durable_runs.py` |
| Story 4 | LLM Council & Session Memory | `core/council.py`, `tests/unit/test_council.py`, `tests/unit/test_council_timer.py`, `cli.py` (idle timer + flush), `bots/example-bot/config.yaml` (`council:` block only) |

**One shared file flag:** Stories 2 and 4 both modify `bots/example-bot/config.yaml`, but each adds a different top-level YAML key (`subagents:` vs `council:`). Zero overlap in content — Phase 6 integration merges both blocks in a single pass.

**`SubagentDefinition` shared type:** Defined in `subagent_proto.py` (Story 1) and imported by Story 2's registry. It is a frozen dataclass with no behavior — Story 2 can stub it during development and import the real one once Story 1 is committed.

**All four stories start immediately, in parallel.**

---

## Story 1: Subagent Protocol & Scratch Dirs

**Type:** Feature  
**Priority:** Should (FR-015)  
**Size:** M (2–3 days)  
**Dependencies:** NONE (independent of Stories 2, 3, 4)

### Description

As a bot developer,  
I want to spawn an isolated subagent process that receives a task via stdin and streams `AgentEvent` JSON lines back via stdout,  
So that complex multi-step pipelines can run in child processes without blowing the parent's context window.

### Technical Context

- **Affected modules:** `monkeybot.core.subagent_proto` (new), `monkeybot.core.runs` (new), `subagents/` (new dir)
- **Design reference:** `1a-discovery.md` "ADR-001", `1b-contracts.md` "core/subagent_proto.py", "core/runs.py", "subagents/researcher.py"
- **Key files to create:**
  - `src/monkeybot/core/subagent_proto.py` — `SubagentDefinition`, `SubagentEnvelope`, `spawn_subagent()`, `read_envelope_from_stdin()`, `emit_event()`
  - `src/monkeybot/core/runs.py` — `create_scratch_dir()`, `cleanup_old_runs()`
  - `subagents/researcher.py` — self-contained example subagent script (validates end-to-end)
  - `tests/unit/test_subagent_proto.py` — 7 unit tests
  - `tests/fixtures/echo_agent.py` — minimal echo script for tests (reads envelope, emits `TurnComplete`)
- **Key files to modify:** none
- **Patterns to follow:**
  - `src/monkeybot/core/events.py` — `event_to_json()`, `event_from_json()`, `AgentEvent` union (already exists; import and use)
  - `src/monkeybot/core/history.py` — `asyncio`-native, no blocking calls
- **Dependencies:** NONE (uses `AgentEvent`, `event_to_json`, `event_from_json` from E1 — already exist; `asyncio.create_subprocess_exec` is stdlib)

### Integration Contracts

**Types defined by this story (exported for Story 2):**

```python
# src/monkeybot/core/subagent_proto.py
from __future__ import annotations
from dataclasses import dataclass, field
from collections.abc import AsyncGenerator
from typing import Any

@dataclass(frozen=True)
class SubagentDefinition:
    """A registered named subagent. Returned by SubagentRegistry.resolve()."""
    name: str
    script: str
    description: str
    skills_path: str
    model: str
    timeout_seconds: int

@dataclass
class SubagentEnvelope:
    """Payload written to child stdin as one JSON line."""
    run_id: str
    parent_run_id: str | None
    agent_name: str | None
    task: str
    context: dict[str, Any]
    skills_path: str
    model: str
    scratch_dir: str

async def spawn_subagent(
    definition_or_script: SubagentDefinition | str,
    task: str,
    context: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    timeout_seconds: int = 300,
) -> AsyncGenerator[AgentEvent, None]:
    """Spawn subagent subprocess; yield its AgentEvent stream."""
    ...

def read_envelope_from_stdin() -> SubagentEnvelope:
    """Read and deserialize SubagentEnvelope from sys.stdin. Used in child scripts."""
    ...

def emit_event(event: AgentEvent) -> None:
    """Write AgentEvent as JSON line to sys.stdout and flush. Used in child scripts."""
    ...
```

```python
# src/monkeybot/core/runs.py
def create_scratch_dir(run_id: str, base_dir: str | None = None) -> str:
    """Create and return isolated temp dir 'monkeybot-run-{run_id}'. Mode 0o700."""
    ...

def cleanup_old_runs(base_dir: str, max_age_days: int = 7) -> int:
    """Delete monkeybot-run-* dirs older than max_age_days. Returns count deleted."""
    ...
```

**Used by Story 2:** `SubagentDefinition` is imported by `subagent_registry.py` — type only, no behavior coupling.  
**Used by cli.py wiring (Phase 6):** `spawn_subagent()` called when main LLM requests a subagent tool call.

### `tests/fixtures/echo_agent.py` — minimal child script

```python
"""Minimal subagent for testing. Reads envelope, emits TurnComplete, exits 0."""
import sys
sys.path.insert(0, "src")
from monkeybot.core.subagent_proto import read_envelope_from_stdin, emit_event
from monkeybot.core.events import TurnComplete
import ulid

envelope = read_envelope_from_stdin()
emit_event(TurnComplete(run_id=envelope.run_id))
```

### Acceptance Criteria

- [ ] **Given** a `SubagentEnvelope`, **When** serialized via `dataclasses.asdict` + `json.dumps` and deserialized, **Then** all fields are equal (round-trip)
- [ ] **Given** `spawn_subagent("tests/fixtures/echo_agent.py", "test task")`, **When** iterated, **Then** yields `SubagentStarted`, then `TurnComplete`, then `SubagentCompleted` in that order
- [ ] **Given** a child script that writes a garbage line to stdout, **When** parent iterates, **Then** yields `ErrorEvent(recoverable=True)` for that line and continues reading
- [ ] **Given** a child script that `time.sleep(999)`, **When** spawned with `timeout_seconds=1`, **Then** parent terminates child and yields `ErrorEvent(message contains "timeout")`
- [ ] **Given** a child script that exits with code 1 without emitting `TurnComplete`, **When** parent finishes iterating, **Then** yields `ErrorEvent(recoverable=True)` then `SubagentCompleted`
- [ ] **Given** `emit_event(TurnComplete())`, **When** called in a child script, **Then** exactly one JSON line is written to stdout and flushed
- [ ] **Given** `create_scratch_dir(run_id)`, **When** called, **Then** directory exists with mode `0o700` and path contains `run_id`
- [ ] **Given** 3 scratch dirs, 2 older than `max_age_days`, **When** `cleanup_old_runs()` called, **Then** returns `2` and only the old dirs are deleted
- [ ] `ruff check` and `mypy --strict` pass on `core/subagent_proto.py` and `core/runs.py`

### `spawn_subagent()` yield sequence (happy path)

```
SubagentStarted(run_id, script, parent_run_id)
[0..N] AssistantDelta / ToolCallStarted / ToolCallResult  ← forwarded from child stdout
TurnComplete(run_id)                                       ← forwarded from child stdout
SubagentCompleted(run_id, scratch_dir)                     ← emitted by parent after child exits
```

### Implementation Notes

- Use `asyncio.create_subprocess_exec(sys.executable, script, stdin=PIPE, stdout=PIPE, stderr=PIPE)` — never shell=True
- Write envelope as one JSON line to stdin; `await proc.stdin.drain(); proc.stdin.close()`
- Read stdout lines with `asyncio.wait_for(proc.stdout.readline(), timeout=...)` — reset timer on each line
- Malformed JSON line → `yield ErrorEvent(recoverable=True)`; log at WARNING with the raw line at DEBUG
- Child stderr → capture and log each line at DEBUG; never yield to parent event stream
- `SubagentDefinition` is exported from this module so Story 2 has a single import target

### Out of Scope

- Registry loading from config (Story 2)
- Durable run persistence (Story 3)
- Council memory (Story 4)
- AgentLoop wiring (Phase 6)

---

## Story 2: Subagent Registry

**Type:** Feature  
**Priority:** Should  
**Size:** S (1–2 days)  
**Dependencies:** NONE (independent of Stories 1, 3, 4 during implementation)

### Description

As a bot operator,  
I want to declare named subagents in `config.yaml` with their own scripts, skills, models, and descriptions,  
So that the main agent knows what specialized agents are available and I can configure their behavior without modifying Python.

### Technical Context

- **Affected modules:** `monkeybot.core.subagent_registry` (new)
- **Design reference:** `1a-discovery.md` "ADR-005", `1b-contracts.md` "core/subagent_registry.py", "config.yaml schema"
- **Key files to create:**
  - `src/monkeybot/core/subagent_registry.py` — `SubagentRegistry` class
  - `tests/unit/test_subagent_registry.py` — 7 unit tests
- **Key files to modify:**
  - `bots/example-bot/config.yaml` — add `subagents:` block (commented example)
- **Patterns to follow:**
  - `src/monkeybot/core/context.py` — `_scan_skills()` pattern for scanning and formatting config into prompt text
  - `src/monkeybot/core/scheduler.py` — startup validation pattern (`validate()`)
- **Dependencies:** NONE — imports `SubagentDefinition` from `core/subagent_proto` (a frozen dataclass; stub during parallel dev if Story 1 not yet committed)

### Integration Contracts

**Types defined by this story:**

```python
# src/monkeybot/core/subagent_registry.py
from monkeybot.core.subagent_proto import SubagentDefinition

class SubagentRegistry:
    def __init__(
        self,
        registry_block: dict[str, Any],   # config["subagents"].get("registry", {})
        *,
        bot_skills_path: str,
        bot_model: str,
        global_timeout: int = 300,
    ) -> None:
        """Validate and load all SubagentDefinitions. Raises ValueError on invalid names."""

    def resolve(self, name: str) -> SubagentDefinition:
        """Return definition for name.
        Raises KeyError: "No subagent '{name}'. Available: {names}" if not found."""

    def all_definitions(self) -> list[SubagentDefinition]:
        """Return all definitions in insertion order."""

    def to_prompt_block(self) -> str:
        """Return markdown table of available subagents for system prompt injection.
        Returns "" if registry is empty."""

    def validate(self) -> list[str]:
        """Check all script paths exist. Returns list of error strings (empty = OK)."""
```

**`to_prompt_block()` output format:**
```
## Available Subagents
| Name | Description |
|------|-------------|
| researcher | Searches the web and summarizes findings on a given topic. |
| reviewer | Reviews a draft for clarity, accuracy, and tone. |
```

**Used by AgentLoop (Phase 6):** `SubagentRegistry` passed as optional constructor arg; `to_prompt_block()` appended to system prompt; `resolve(name)` called when main LLM requests a named subagent.

### `config.yaml` subagent schema

```yaml
subagents:
  timeout_seconds: 300
  registry:
    researcher:
      script: "subagents/researcher.py"
      description: "Searches the web and summarizes findings on a given topic."
      skills_path: ".agents/skills/research"   # optional
      model: "gemini-2.0-pro"                  # optional
      timeout_seconds: 120                     # optional
    reviewer:
      script: "subagents/reviewer.py"
      description: "Reviews a draft for clarity, accuracy, and tone."
```

### Acceptance Criteria

- [ ] **Given** a registry block with 2 entries, **When** `resolve("researcher")` called, **Then** returns `SubagentDefinition` with all fields populated (including bot defaults for missing `model`/`skills_path`)
- [ ] **Given** `resolve("unknown")`, **When** called, **Then** raises `KeyError` with message listing available names
- [ ] **Given** a definition with no `model` in yaml, **When** registry constructed with `bot_model="gemini-2.0-flash"`, **Then** `definition.model == "gemini-2.0-flash"`
- [ ] **Given** 2 registered subagents, **When** `to_prompt_block()` called, **Then** returns markdown string containing both names and descriptions
- [ ] **Given** empty `registry_block`, **When** `to_prompt_block()` called, **Then** returns `""`
- [ ] **Given** a definition with a non-existent script path, **When** `validate()` called, **Then** returns list with one error string naming the subagent and path
- [ ] **Given** all script paths exist, **When** `validate()` called, **Then** returns `[]`
- [ ] **Given** a name containing uppercase or spaces, **When** registry constructed, **Then** raises `ValueError` at init (fail-fast)
- [ ] `ruff check` and `mypy --strict` pass on `core/subagent_registry.py`

### Implementation Notes

- Regex for name validation: `^[a-z0-9][a-z0-9-]*$`
- `validate()` resolves script paths relative to `Path.cwd()` — document this in the docstring
- `to_prompt_block()` sorts definitions by insertion order (dict is ordered in Python 3.7+)
- `all_definitions()` returns a copy of the internal list — not the mutable backing store

### Out of Scope

- Spawning subagents (Story 1)
- Durable persistence (Story 3)
- Council (Story 4)
- AgentLoop constructor injection (Phase 6)

---

## Story 3: Durable Run Store

**Type:** Feature  
**Priority:** Should (FR-016, US-09)  
**Size:** S (1–2 days)  
**Dependencies:** NONE (independent of Stories 1, 2, 4)

### Description

As a bot operator,  
I want interrupted subagent pipelines to be recoverable via `pending_runs()`,  
So that a container crash mid-pipeline does not require manual intervention or data loss.

### Technical Context

- **Affected modules:** `monkeybot.core.durable_runs` (new)
- **Design reference:** `1a-discovery.md` "ADR-002", `1b-contracts.md` "core/durable_runs.py", "durable_runs table schema"
- **Key files to create:**
  - `src/monkeybot/core/durable_runs.py` — `DurableRunStore` class
  - `tests/unit/test_durable_runs.py` — 7 unit tests against real SQLite
- **Key files to modify:** none
- **Patterns to follow:**
  - `src/monkeybot/core/history.py` — identical `__init__(db_path)` pattern, WAL mode, `CREATE TABLE IF NOT EXISTS`, `aiosqlite` open-per-call, `Path.mkdir(parents=True)`
- **Dependencies:** NONE — `aiosqlite` already in deps; `ulid` already in deps

### Integration Contracts

**Types defined by this story:**

```python
# src/monkeybot/core/durable_runs.py
from typing import Any

class DurableRunStore:
    def __init__(self, db_path: str) -> None:
        """Accept bare file path (e.g. 'data/monkeybot.db'). Same pattern as ConversationHistory."""

    async def init(self) -> None:
        """Create durable_runs table + indexes. Safe to call multiple times (IF NOT EXISTS)."""

    async def record_started(
        self,
        run_id: str,
        script: str,
        scratch_dir: str,
        parent_run_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """INSERT OR IGNORE row with status='running'. Call BEFORE spawning subprocess."""

    async def record_completed(self, run_id: str) -> None:
        """Set status='completed', completed_at=now_ms. No-op if run_id unknown or already terminal."""

    async def record_failed(self, run_id: str, error_msg: str) -> None:
        """Set status='failed', error_msg, completed_at=now_ms. No-op if already terminal."""

    async def pending_runs(self) -> list[dict[str, Any]]:
        """Return all rows where status='running'.
        Keys: run_id, agent_name, script, scratch_dir, parent_run_id, started_at.
        Returns [] if none."""
```

**Used by cli.py wiring (Phase 6):** `DurableRunStore` instantiated at startup; `record_started` / `record_completed` / `record_failed` called from the spawn_subagent caller; `pending_runs()` optionally exposed via a future `monkeybot runs` CLI command.

### `durable_runs` table schema

```sql
CREATE TABLE IF NOT EXISTS durable_runs (
    run_id          TEXT    PRIMARY KEY,
    parent_run_id   TEXT,
    agent_name      TEXT,
    script          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'running',
    scratch_dir     TEXT    NOT NULL,
    error_msg       TEXT,
    started_at      INTEGER NOT NULL,
    completed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_durable_runs_status ON durable_runs(status);
CREATE INDEX IF NOT EXISTS idx_durable_runs_parent ON durable_runs(parent_run_id);
```

### Acceptance Criteria

- [ ] **Given** `record_started(run_id="01ABC", script="x.py", scratch_dir="/tmp/x")`, **When** called, **Then** one row exists with `status='running'` and `completed_at=NULL`
- [ ] **Given** `record_started` called twice with the same `run_id`, **When** both complete, **Then** exactly 1 row exists (`INSERT OR IGNORE`)
- [ ] **Given** a `running` row, **When** `record_completed(run_id)` called, **Then** `status='completed'` and `completed_at` is set (non-NULL)
- [ ] **Given** a `running` row, **When** `record_failed(run_id, "timeout")` called, **Then** `status='failed'`, `error_msg='timeout'`, `completed_at` set
- [ ] **Given** a `completed` row, **When** `record_failed(run_id, "retry")` called, **Then** row unchanged (idempotent — terminal state preserved)
- [ ] **Given** 1 `running` and 1 `completed` row, **When** `pending_runs()` called, **Then** returns list with exactly 1 dict (the running row)
- [ ] **Given** all rows terminal, **When** `pending_runs()` called, **Then** returns `[]`
- [ ] **Given** DB file does not exist, **When** `init()` called, **Then** file and table are created (parent dirs via `Path.mkdir(parents=True, exist_ok=True)`)
- [ ] `ruff check` and `mypy --strict` pass on `core/durable_runs.py`

### Implementation Notes

- `started_at` and `completed_at` are Unix milliseconds (consistent with `messages.created_at`)
- `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL` in `init()` (same as `history.py`)
- `record_completed` and `record_failed` use `UPDATE ... WHERE run_id=? AND status='running'` — this naturally makes them no-ops on terminal rows without an extra read
- `pending_runs()` returns plain dicts, not dataclasses, for simplicity — consistent with aiosqlite row pattern

### Out of Scope

- Spawning subagents (Story 1)
- Registry (Story 2)
- Council (Story 4)
- `monkeybot runs` CLI command (future epic)
- Auto-retry of failed runs (operator responsibility)

---

## Story 4: LLM Council & Session Memory

**Type:** Feature  
**Priority:** Should (FR-017)  
**Size:** M (2–3 days)  
**Dependencies:** NONE (independent of Stories 1, 2, 3 during implementation)

### Description

As a bot operator,  
I want the agent to automatically consolidate learnings from each session into structured memory files,  
So that the agent becomes smarter over time without any manual curation — preferences are remembered, facts accumulate, and duplicates are eliminated.

### Technical Context

- **Affected modules:** `monkeybot.core.council` (new), `monkeybot.cli` (idle timer + flush)
- **Design reference:** `1a-discovery.md` "ADR-003", `1b-contracts.md` "core/council.py", "council idle timer wire-up", "config.yaml schema"
- **Key files to create:**
  - `src/monkeybot/core/council.py` — `MANAGED_CATEGORIES`, `COUNCIL_PROMPT`, `run_council()`, `_load_existing_categories()`, `_parse_council_sections()`
  - `tests/unit/test_council.py` — 11 unit tests
  - `tests/unit/test_council_timer.py` — 6 unit tests
- **Key files to modify:**
  - `src/monkeybot/cli.py` — add `_council_timers`, `_background_tasks`, `_on_turn_complete` idle timer logic, `_flush_council_on_shutdown()`
  - `bots/example-bot/config.yaml` — add `council:` block (commented)
- **Patterns to follow:**
  - `src/monkeybot/core/memory.py` — `save_memory()` for writing files
  - `src/monkeybot/core/history.py` — `load()` to get conversation text for council
  - `src/monkeybot/core/scheduler.py` — asyncio task lifecycle (cancel + await pattern)
- **Dependencies:** NONE — `Provider` Protocol and `save_memory` are E1 artifacts; `history.load()` is E1

### Integration Contracts

**Types and constants defined by this story:**

```python
# src/monkeybot/core/council.py
from monkeybot.core.provider import Provider

MANAGED_CATEGORIES: tuple[str, ...] = (
    "user-preferences",
    "key-facts",
    "open-questions",
)

COUNCIL_PROMPT: str  # Template with {existing_memories_block} and {conversation_text}

async def run_council(
    conversation_text: str,
    memory_path: str,
    provider: Provider,
    model: str,
    session_id: str,
) -> list[str]:
    """Read existing category files → call LLM → write merged category files + dated session file.
    Returns list of filenames written. Never raises — all errors logged and swallowed."""

def _load_existing_categories(memory_path: str) -> dict[str, str]:
    """Read each MANAGED_CATEGORIES file. Returns {name: content or ""}. Never raises."""

def _parse_council_sections(response: str) -> dict[str, str]:
    """Split LLM response on '## ' headers. Returns {slug: content}. Never raises."""
```

**Timer state (in `cli.py`, not in council.py):**
```python
# src/monkeybot/cli.py additions
_council_timers: dict[str, asyncio.Task[None]] = {}
_background_tasks: set[asyncio.Task[None]] = set()

async def _on_turn_complete(session_id: str, tc: TurnComplete) -> None: ...
async def _flush_council_on_shutdown() -> None: ...
```

**Used by AgentLoop (Phase 6):** `_on_turn_complete` passed as the `on_turn_complete` callback to `AgentLoop.__init__`.

### `COUNCIL_PROMPT` template

```
You are the LLM Memory Council. Your job is to maintain the agent's long-term memory.

## Existing Memory
{existing_memories_block}

## Session Conversation
{conversation_text}

## Instructions
Produce updated memory by merging existing memory with new insights from the session above.

Rules:
- PRESERVE all existing facts unless this session contradicts or supersedes them.
- ADD new facts, preferences, and insights from this session.
- NEVER duplicate a fact — if it already exists, do not repeat it.
- CONSOLIDATE redundant or overlapping entries into a single concise bullet.
- REMOVE entries that were explicitly resolved or corrected in this session.
- Keep each section concise — bullet points only, no prose.
- Output ONLY the sections below — no preamble, no commentary, no extra text.

## Summary
<2-4 sentences summarizing ONLY this session>

## user-preferences
<complete merged bullet list — user style, habits, communication preferences>

## key-facts
<complete merged bullet list — facts, decisions, outcomes, domain knowledge>

## open-questions
<complete merged bullet list — unresolved items; omit resolved ones>
```

### Acceptance Criteria

- [ ] **Given** empty `conversation_text`, **When** `run_council()` called, **Then** returns `[]` and provider is NOT called
- [ ] **Given** a 10-turn conversation, **When** `run_council()` called with `FakeProvider` returning all 4 sections, **Then** 4 files written: `{date}-session-*.md`, `user-preferences.md`, `key-facts.md`, `open-questions.md`
- [ ] **Given** existing `user-preferences.md` with content "- Writes in ALL CAPS", **When** council runs and LLM output includes that fact plus a new one, **Then** `user-preferences.md` contains both facts (read-merge-write verified)
- [ ] **Given** council called twice with the same fact, **When** second call runs with `FakeProvider` that dedups, **Then** fact appears exactly once in the category file
- [ ] **Given** LLM response omits `## open-questions`, **When** council runs, **Then** existing `open-questions.md` on disk is NOT overwritten
- [ ] **Given** provider raises an exception, **When** `run_council()` called, **Then** returns `[]`, no crash, error logged
- [ ] **Given** `idle_seconds=1` and 3 turns sent in rapid succession to `_on_turn_complete`, **When** 1 second passes after last turn, **Then** council is called exactly once (debounce verified)
- [ ] **Given** a pending idle timer, **When** new turn arrives, **Then** existing timer is cancelled and fresh timer starts (reset verified)
- [ ] **Given** 2 sessions with pending timers, **When** `_flush_council_on_shutdown()` called, **Then** both timers cancelled and `run_council()` called once per session
- [ ] **Given** `council.enabled: false`, **When** `_on_turn_complete` fires, **Then** no timer is created and `_council_timers` remains empty
- [ ] `ruff check` and `mypy --strict` pass on `core/council.py`

### `_flush_council_on_shutdown()` contract

```python
async def _flush_council_on_shutdown() -> None:
    """Cancel all pending idle timers; run council immediately for each pending session.
    Called from _serve_async try/finally before process exit.
    Never raises — errors logged per session."""
    for session_id, task in list(_council_timers.items()):
        task.cancel()
        try:
            history_msgs = await history.load(session_id)
            text = "\n".join(f"{m.role}: {m.content}" for m in history_msgs)
            await run_council(text, memory_path, provider, council_model, session_id)
        except Exception:
            log.exception("flush_council failed session_id=%s", session_id)
    _council_timers.clear()
```

### `config.yaml` council schema

```yaml
council:
  enabled: true           # default false — must opt in
  idle_seconds: 300       # seconds of inactivity before council fires; default 300
  model: "gemini-2.0-flash"   # optional; falls back to model.default
```

### Implementation Notes

- `_parse_council_sections` splits on `\n## ` (newline + header) — handles multi-line content in each section cleanly
- Section header → slug conversion: `header.lower().strip().replace(" ", "-")`
- `run_council` writes dated session file from the `## Summary` section only — not the full response — to keep session files concise
- The `asyncio.create_task` GC risk: store task in `_background_tasks` set, add `done_callback` to discard it — see 1c-operations.md "Risk: asyncio.create_task GC"
- `idle_seconds` validation: must be positive integer; `ValueError` at startup if invalid; log WARNING if < 60 (too aggressive for interactive bots)

### Out of Scope

- Subagent spawning (Story 1)
- Registry (Story 2)
- Durable run persistence (Story 3)
- HITL approval flow (deferred — PRT open item)
- Encryption of memory files (E5)
- Cost instrumentation for council LLM calls (future)

---

## Phase 6 Integration Points

When all four stories are complete, Phase 6 performs the following wiring — all additive, no story content is modified:

1. **`AgentLoop.__init__`** — add optional `registry: SubagentRegistry | None = None` param; append `registry.to_prompt_block()` to system prompt in `run()`
2. **`_dispatch_tool` in `loop.py`** — wire `spawn_subagent` as a callable tool; call `DurableRunStore.record_started/completed/failed` around spawn
3. **`cli.py` `_serve_async`** — construct `SubagentRegistry` from config; construct `DurableRunStore` and call `init()`; call `cleanup_old_runs()` at startup; add `await _flush_council_on_shutdown()` to `finally` block
4. **`bots/example-bot/config.yaml`** — merge Story 2's `subagents:` block and Story 4's `council:` block into one file
5. **`tests/integration/test_pipeline.py`** — end-to-end: parent spawns researcher → council runs after idle timer → memory files written
