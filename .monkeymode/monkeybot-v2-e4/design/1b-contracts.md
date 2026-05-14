# Design: monkeybot-v2-e4 — Subagents, Durability & LLM Council
## Phase 1B: Detailed Contracts

**Date:** 2026-05-13  
**Status:** Phase 1B — API Contracts & Integration Points  
**Version:** 1.0

---

## Public Python API Contracts

E4 has no HTTP endpoints — it's a library feature. The "API" is the public function/class surface across five new modules.

---

### `core/subagent_registry.py`

#### `SubagentDefinition` (dataclass)

```python
@dataclass
class SubagentDefinition:
    name: str                    # registry key; alphanumeric + hyphens only
    script: str                  # path to subagent script, relative to bot dir
    description: str             # one sentence shown to the main LLM
    skills_path: str             # resolved at init; falls back to bot's global skills_path
    model: str                   # resolved at init; falls back to bot's default model
    timeout_seconds: int         # resolved at init; falls back to subagents.timeout_seconds
```

All fields are required on the resolved dataclass. `SubagentRegistry.__init__` handles the fallback resolution so callers always receive a fully populated definition.

---

#### `SubagentRegistry`

```python
class SubagentRegistry:
    def __init__(
        self,
        registry_block: dict[str, Any],   # config["subagents"]["registry"]
        *,
        bot_skills_path: str,
        bot_model: str,
        global_timeout: int = 300,
    ) -> None: ...

    def resolve(self, name: str) -> SubagentDefinition:
        """Return definition for *name*.
        Raises KeyError with a clear message listing available names if not found."""

    def all_definitions(self) -> list[SubagentDefinition]:
        """Return all registered definitions in insertion order."""

    def to_prompt_block(self) -> str:
        """Return a markdown block for injection into the system prompt.

        Format:
        ## Available Subagents
        | Name | Description |
        |------|-------------|
        | researcher | Searches the web and summarizes findings on a given topic. |
        | reviewer   | Reviews a draft for clarity, accuracy, and tone.           |

        Returns empty string if registry is empty.
        """

    def validate(self) -> list[str]:
        """Check all script paths exist relative to bot dir.
        Returns list of error strings. Empty list means all OK.
        Caller decides whether to raise or log on startup."""
```

**Behavior contract:**

| Scenario | Expected behaviour |
|---|---|
| `registry_block` is empty or absent | `all_definitions()` returns `[]`; `to_prompt_block()` returns `""` |
| `resolve("unknown")` | Raises `KeyError: "No subagent 'unknown'. Available: researcher, reviewer"` |
| `validate()` on missing script | Returns `["subagent 'researcher': script 'subagents/researcher.py' not found"]` |
| Partial definition (no `model` in yaml) | Falls back to `bot_model` during `__init__` |
| `name` contains invalid chars | Raises `ValueError` at init (fail-fast) |

---

### `core/subagent_proto.py`

#### `SubagentEnvelope` (dataclass)

```python
@dataclass
class SubagentEnvelope:
    run_id: str                  # ULID for this subagent run
    parent_run_id: str | None    # AgentLoop's current run_id; None for top-level
    agent_name: str | None       # registry name if spawned via registry; None for ad-hoc
    task: str                    # natural language task (set by the main LLM)
    context: dict[str, Any]      # arbitrary caller-provided key-value context
    skills_path: str             # resolved from definition or bot default
    model: str                   # resolved from definition or bot default
    scratch_dir: str             # absolute path to isolated working dir for this run
```

Serialized to/from JSON via `dataclasses.asdict` + `json.dumps` (one line). No envelope-specific `event_to_json` — it's a separate type from `AgentEvent`.

---

#### `spawn_subagent()`

```python
async def spawn_subagent(
    definition_or_script: SubagentDefinition | str,
    task: str,
    context: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    timeout_seconds: int = 300,
) -> AsyncGenerator[AgentEvent, None]:
    """Spawn a subagent subprocess and yield its AgentEvent stream.

    Steps:
    1. Resolve script path and timeout from definition (or use raw str as script).
    2. call create_scratch_dir(run_id) → scratch_dir.
    3. Build SubagentEnvelope.
    4. asyncio.create_subprocess_exec(sys.executable, script,
           stdin=PIPE, stdout=PIPE, stderr=PIPE)
    5. Write envelope JSON line to stdin; close stdin.
    6. Yield SubagentStarted.
    7. Read stdout line-by-line under asyncio.wait_for(timeout):
       - Deserialize via event_from_json(); yield each AgentEvent.
       - On malformed line: yield ErrorEvent(recoverable=True); continue.
       - On TurnComplete: yield it; break (subprocess is done).
    8. On timeout: terminate subprocess; yield ErrorEvent("Subagent timeout", recoverable=True).
    9. Await subprocess.wait(); yield SubagentCompleted(run_id, scratch_dir).
    """
```

**Yield sequence (happy path):**
```
SubagentStarted
AssistantDelta (0..N — forwarded from child stdout)
ToolCallStarted / ToolCallResult (0..N — forwarded from child stdout)
TurnComplete (from child; signals child is done)
SubagentCompleted (emitted by parent after child exits)
```

**Error cases:**

| Scenario | Parent behaviour |
|---|---|
| Child stdout: malformed JSON line | `yield ErrorEvent(recoverable=True)`; continue reading |
| Child exits non-zero without `TurnComplete` | `yield ErrorEvent("Subagent exited with code N", recoverable=True)` then `SubagentCompleted` |
| Timeout reached | Terminate child; `yield ErrorEvent("Subagent timeout", recoverable=True)` then `SubagentCompleted` |
| Script not found (`FileNotFoundError`) | `yield ErrorEvent("Script not found: {script}", recoverable=False)` |
| Child writes to stderr | Captured silently; logged at DEBUG level; never yielded to parent event stream |

---

#### `read_envelope_from_stdin() -> SubagentEnvelope`

```python
def read_envelope_from_stdin() -> SubagentEnvelope:
    """Read and deserialize one JSON line from sys.stdin.
    Called at the top of every subagent script.
    Raises ValueError if stdin is empty or JSON is malformed."""
```

---

#### `emit_event(event: AgentEvent) -> None`

```python
def emit_event(event: AgentEvent) -> None:
    """Write event as a JSON line to sys.stdout and flush.
    Called by child scripts to stream events back to the parent."""
```

stdout is exclusively for event lines. Child scripts MUST use `sys.stderr` or file logging for debug output.

---

### `core/runs.py`

```python
def create_scratch_dir(run_id: str, base_dir: str | None = None) -> str:
    """Create and return an isolated temp directory for *run_id*.

    Path: {base_dir or tempfile.gettempdir()}/monkeybot-run-{run_id}
    Created with mode 0o700 (owner-only).
    Returns absolute path string.
    Never raises — if creation fails, propagates OSError to caller.
    """

def cleanup_old_runs(base_dir: str, max_age_days: int = 7) -> int:
    """Delete monkeybot-run-* dirs under base_dir older than max_age_days.
    Returns count of directories deleted.
    Silently skips dirs that cannot be removed (permissions, still in use).
    """
```

---

### `core/durable_runs.py`

#### `DurableRunStore`

```python
class DurableRunStore:
    def __init__(self, db_path: str) -> None:
        """Accept bare file path (same pattern as ConversationHistory)."""

    async def init(self) -> None:
        """Create durable_runs table and indexes. Safe to call multiple times."""

    async def record_started(
        self,
        run_id: str,
        script: str,
        scratch_dir: str,
        parent_run_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Insert a row with status='running'. Called BEFORE subprocess spawn."""

    async def record_completed(self, run_id: str) -> None:
        """Set status='completed', completed_at=now. Idempotent (no-op if already terminal)."""

    async def record_failed(self, run_id: str, error_msg: str) -> None:
        """Set status='failed', error_msg, completed_at=now. Idempotent."""

    async def pending_runs(self) -> list[dict[str, Any]]:
        """Return all rows where status='running'.
        Each dict has keys: run_id, agent_name, script, scratch_dir, parent_run_id, started_at.
        Returns [] if none."""
```

**Behaviour contract:**

| Scenario | Expected behaviour |
|---|---|
| `record_started` called twice with same `run_id` | Second call is no-op (`INSERT OR IGNORE`) |
| `record_completed` on unknown `run_id` | No-op (0 rows updated); no error raised |
| `record_failed` on already-completed row | No-op; existing terminal state preserved |
| DB file doesn't exist yet | `init()` creates it (parent dirs created via `Path.mkdir(parents=True)`) |
| `pending_runs()` after clean shutdown | Returns `[]` (all rows transitioned to terminal) |
| `pending_runs()` after crash (rows still `running`) | Returns the interrupted run rows for recovery |

---

### `core/council.py`

#### Design: Read-Merge-Write

The council runs in three logical phases every time it is called:

1. **Read** — load the full content of all existing managed category files from disk.
2. **Merge** — pass both the existing content AND the new conversation to the LLM in a single prompt. The LLM is responsible for producing complete, deduplicated, merged output.
3. **Write** — overwrite each category file with the LLM's merged output. This is safe because the LLM output already includes everything that was in the old file.

This approach means deduplication happens inside the LLM's reasoning, not in Python parsing logic. No diffing code. No risk of the `user-preferences.md` being created twice.

---

#### `MANAGED_CATEGORIES`

```python
MANAGED_CATEGORIES: tuple[str, ...] = (
    "user-preferences",   # user style, habits, communication preferences
    "key-facts",          # factual knowledge, decisions, domain context
    "open-questions",     # unresolved questions; items removed when resolved
)
```

These files live directly under `memory_path/`. They are **always overwritten** (never appended) by the council because each write is already the fully merged result. Dated session files are separate and immutable.

---

#### `COUNCIL_PROMPT`

```python
COUNCIL_PROMPT = """\
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
- If a category has nothing new and nothing to change, reproduce its existing content unchanged.
- Output ONLY the sections below — no preamble, no commentary, no extra text.

## Summary
<2-4 sentences summarizing ONLY this session — what happened, what was requested, what was decided>

## user-preferences
<complete merged bullet list — user style, habits, communication preferences>

## key-facts
<complete merged bullet list — facts, decisions, outcomes, domain knowledge>

## open-questions
<complete merged bullet list — unresolved questions and follow-up items; omit bullets that are now resolved>
"""
```

---

#### `run_council()`

```python
async def run_council(
    conversation_text: str,
    memory_path: str,
    provider: Provider,
    model: str,
    session_id: str,
) -> list[str]:
    """Call the LLM council and write structured memory files.

    Steps:
    1. If conversation_text is empty, return [] without calling provider.
    2. Load existing managed category files:
       existing: dict[str, str] = _load_existing_categories(memory_path)
       Each value is the file's current content, or "" if the file doesn't exist yet.
    3. Format existing_memories_block as:
       "### user-preferences\n{content or '(no existing memories)'}\n\n### key-facts\n..."
    4. Format COUNCIL_PROMPT with both blocks.
    5. Call provider.stream([Message(role="user", content=prompt)], [], model=model).
    6. Collect full text response.
    7. Parse into sections: sections = _parse_council_sections(response)
       → dict[str, str] keyed by section header (lowercase, hyphenated)
    8. Write files:
       - {YYYY-MM-DD}-session-{session_id[:8]}.md  ← sections["summary"] only
       - user-preferences.md  ← sections["user-preferences"] (skip if empty)
       - key-facts.md         ← sections["key-facts"] (skip if empty)
       - open-questions.md    ← sections["open-questions"] (skip if empty)
    9. Return list of filenames written.

    Never raises — all errors logged via logging.getLogger("monkeybot.council") and swallowed.
    Caller fire-and-forgets via asyncio.create_task().
    """
```

---

#### Private helpers

```python
def _load_existing_categories(memory_path: str) -> dict[str, str]:
    """Read each MANAGED_CATEGORIES file from memory_path.
    Returns {category_name: content} — content is "" if file doesn't exist.
    Never raises.
    """

def _parse_council_sections(response: str) -> dict[str, str]:
    """Split council LLM response on '## ' headers.
    Returns {header_slug: content} where header_slug is the header text
    lowercased and spaces replaced with hyphens.
    e.g. "## Key Facts\n- item" → {"key-facts": "- item"}
    Content is stripped. Empty sections produce "" values (not omitted).
    """
```

---

**Error handling:**

| Scenario | Behaviour |
|---|---|
| `conversation_text` is empty string | Returns `[]` immediately; provider not called |
| Provider call raises | Logged at ERROR; returns `[]`; no crash |
| Response missing an expected section | That category file is not written; existing file on disk is untouched |
| `save_memory` fails (disk full, permissions) | Logged; skips that file; continues with others |
| Existing category file is unreadable | `_load_existing_categories` returns `""` for that entry; logged at WARNING |
| LLM produces duplicate facts in output | Accepted — council prompt instructs against it but code does not enforce; rare in practice |

---

### `config.yaml` schema (full subagent + council block)

```yaml
subagents:
  timeout_seconds: 300              # global default; int, required if registry is present
  registry:                         # optional block; omit to disable named subagents
    researcher:
      script: "subagents/researcher.py"
      description: "Searches the web and summarizes findings on a given topic."
      skills_path: ".agents/skills/research"  # optional; falls back to bot default
      model: "gemini-2.0-pro"                 # optional; falls back to bot default
      timeout_seconds: 120                    # optional; overrides global default

    reviewer:
      script: "subagents/reviewer.py"
      description: "Reviews a draft for clarity, accuracy, and tone."
      # skills_path + model omitted → use bot defaults

council:
  enabled: true           # bool; default false — must opt in
  idle_seconds: 300       # seconds of inactivity before council fires; default 300 (5 min)
  model: "gemini-2.0-flash"   # optional; falls back to bot default model
```

**Validation rules:**
- `subagents.registry` is optional; if absent, `SubagentRegistry` is constructed with an empty dict
- `description` is required per entry (used in system prompt — must not be blank)
- `script` is required per entry; `validate()` warns at startup if path doesn't exist
- `council.enabled: false` (or absent) → `run_council` is never called; no timers created; no performance impact
- `council.idle_seconds` must be a positive integer; invalid → `ValueError` at startup; minimum recommended value is 60
- `council.model` is optional; falls back to `model.default` in config

---

### `subagents/researcher.py` — behaviour contract

This is a standalone script, not a library. Its contract is behavioural:

```
Entry:  reads SubagentEnvelope from stdin via read_envelope_from_stdin()
        → accesses envelope.task, envelope.skills_path, envelope.scratch_dir

Steps:
  1. emit_event(SubagentStarted(run_id=envelope.run_id, script=__file__))  [optional signal]
  2. Load skills from envelope.skills_path using list_skills tool pattern
  3. Run a single-turn research loop (no sub-spawning):
     - emit_event(AssistantDelta(...)) for each text chunk
     - emit_event(ToolCallStarted / ToolCallResult) for each tool call
  4. emit_event(TurnComplete(run_id=envelope.run_id, ...))

Exit:   sys.exit(0) on success; sys.exit(1) on unhandled exception
        All exceptions caught at top level → emit ErrorEvent → sys.exit(1)
```

stdout: exclusively AgentEvent JSON lines  
stderr: all logging, debug output  

---

## Integration Points

### Events Consumed

| Event | Source | Trigger | Action |
|---|---|---|---|
| `TurnComplete` | `AgentLoop.run()` finally block | Every completed agent turn | `asyncio.create_task(run_council(...))` if `council.enabled` |

### Events Published (by parent, wrapping child events)

| Event | Trigger | Fields |
|---|---|---|
| `SubagentStarted` | `spawn_subagent()` before reading stdout | `run_id`, `script`, `parent_run_id` |
| `AssistantDelta` | Forwarded from child stdout | Passthrough |
| `ToolCallStarted` | Forwarded from child stdout | Passthrough |
| `ToolCallResult` | Forwarded from child stdout | Passthrough |
| `TurnComplete` | Forwarded from child stdout | Passthrough (child's run_id) |
| `SubagentCompleted` | After child process exits | `run_id`, `scratch_dir` |
| `ErrorEvent` | Timeout, crash, malformed output | `message`, `recoverable` |

### Wire-up in `AgentLoop`

**`__init__` — registry injection:**
```python
class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        history: ConversationHistory,
        inspectors: list[ToolInspector],
        config: dict[str, Any],
        on_turn_complete: Callable[[str, TurnComplete], Awaitable[None]] | None = None,
        registry: SubagentRegistry | None = None,   # NEW: optional, default None
    ) -> None:
        self._registry = registry
        # ... existing init ...
```

**System prompt enrichment (in `load_turn_context` call or before it):**
```python
# In AgentLoop._build_system_context() or appended to ctx.build_system_prompt():
if self._registry:
    extra = self._registry.to_prompt_block()
    # appended to system prompt string before provider.stream() call
```

**`_on_turn_complete` hook — council idle timer (debounce):**

The gateway layer owns a `_council_timers: dict[str, asyncio.Task]` and `_background_tasks: set[asyncio.Task]` at module level.

```python
_council_timers: dict[str, asyncio.Task] = {}
_background_tasks: set[asyncio.Task] = set()

async def _on_turn_complete(session_id: str, tc: TurnComplete) -> None:
    if not config.get("council", {}).get("enabled"):
        return

    # Cancel any existing idle timer for this session — user is still active
    existing = _council_timers.pop(session_id, None)
    if existing and not existing.done():
        existing.cancel()

    idle_seconds: int = config.get("council", {}).get("idle_seconds", 300)

    async def _fire_after_idle() -> None:
        await asyncio.sleep(idle_seconds)
        _council_timers.pop(session_id, None)
        history_msgs = await history.load(session_id)
        text = "\n".join(f"{m.role}: {m.content}" for m in history_msgs)
        await run_council(text, memory_path, provider, council_model, session_id)

    task = asyncio.create_task(_fire_after_idle())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    _council_timers[session_id] = task


async def _flush_council_on_shutdown() -> None:
    """Called on SIGTERM. Cancels all idle timers and runs council immediately
    for each pending session so no memory is lost on graceful shutdown."""
    for session_id, task in list(_council_timers.items()):
        task.cancel()
        history_msgs = await history.load(session_id)
        text = "\n".join(f"{m.role}: {m.content}" for m in history_msgs)
        await run_council(text, memory_path, provider, council_model, session_id)
    _council_timers.clear()
```

**Behaviour contract for the idle timer:**

| Scenario | Behaviour |
|---|---|
| Turn arrives while timer is running | Existing timer cancelled; new timer started from zero |
| User idle for `idle_seconds` | Timer fires; council runs once for the full session history |
| User sends message 4 minutes into a 5-minute timer | Timer resets to 5 minutes from now |
| SIGTERM received with 2 sessions pending | `_flush_council_on_shutdown()` cancels both timers and runs council synchronously for each |
| Process crash (not SIGTERM) mid-sleep | Timer lost; council not written for that session; accepted |
| `council.enabled: false` | No timers created; `_council_timers` always empty |

**`DurableRunStore` wire-up — in `spawn_subagent` caller or `_dispatch_tool`:**
```python
# When the LLM calls the spawn_subagent tool:
run_id = str(ulid.new())
await durable_store.record_started(run_id, script, scratch_dir, parent_run_id, agent_name)
async for event in spawn_subagent(definition, task, context, parent_run_id=run_id):
    yield event
    if isinstance(event, SubagentCompleted):
        await durable_store.record_completed(run_id)
    elif isinstance(event, ErrorEvent) and not event.recoverable:
        await durable_store.record_failed(run_id, event.message)
```

### Dependency on E1–E3 Contracts

| E1–E3 Symbol | How E4 uses it |
|---|---|
| `AgentEvent` union + `event_to_json/from_json` | Subagent protocol serialization/deserialization |
| `SubagentStarted`, `SubagentCompleted` | Already defined in `events.py`; E4 emits them |
| `ConversationHistory._db_path` pattern | `DurableRunStore.__init__(db_path)` follows the same |
| `save_memory(memory_path, filename, content)` | `run_council()` writes memory files via this function |
| `Provider` Protocol | `run_council()` calls `provider.stream()` — same interface |
| `AgentLoop._on_turn_complete` callback | Council is triggered from this hook (added in E1) |
| `config["model"]` default | Fallback for `SubagentDefinition.model` and `council.model` |

### External Dependencies

| Dependency | Usage | Optional | Failure handling |
|---|---|---|---|
| `aiosqlite` | `durable_runs` table | No (core dep) | Propagate; caller logs and continues |
| `asyncio.create_subprocess_exec` | Process spawn | No (stdlib) | `FileNotFoundError` → `ErrorEvent` |
| `sys.stdin / sys.stdout` | Envelope + event wire | No (stdlib) | — |
| Python `tempfile` | Scratch dir creation | No (stdlib) | `OSError` → propagated |

---

## Testing Strategy

### Unit Testing

**Coverage target:** 100% critical paths on all new modules.

**`tests/unit/test_subagent_registry.py`**

| Test | Description | Mock |
|---|---|---|
| `test_resolve_known_name` | Returns correct `SubagentDefinition` | No mock |
| `test_resolve_unknown_name` | Raises `KeyError` with helpful message | No mock |
| `test_fallback_to_bot_defaults` | Missing `model`/`skills_path` fall back to bot values | No mock |
| `test_to_prompt_block_format` | Returns valid markdown table with all names | No mock |
| `test_to_prompt_block_empty` | Returns `""` when registry is empty | No mock |
| `test_validate_missing_script` | Returns error string for missing script path | `tmp_path` fixture |
| `test_validate_all_present` | Returns `[]` when all scripts exist | `tmp_path` fixture |

**`tests/unit/test_subagent_proto.py`**

| Test | Description | Mock |
|---|---|---|
| `test_envelope_json_roundtrip` | `SubagentEnvelope` serializes and deserializes correctly | No mock |
| `test_spawn_echo_script` | Spawn a minimal echo script; parent receives `TurnComplete` | Real subprocess (echo script fixture) |
| `test_spawn_malformed_line` | Script emits garbage line; parent yields `ErrorEvent`, continues | Real subprocess |
| `test_spawn_timeout` | Script hangs; parent terminates and yields timeout `ErrorEvent` | Real subprocess + tiny timeout |
| `test_spawn_nonzero_exit` | Script exits 1 without `TurnComplete`; parent yields `ErrorEvent` | Real subprocess |
| `test_emit_event_writes_json_line` | `emit_event()` writes correct JSON line to stdout | Capture stdout |
| `test_read_envelope_from_stdin` | `read_envelope_from_stdin()` parses correctly | Patch `sys.stdin` |

**`tests/unit/test_durable_runs.py`**

| Test | Description | Mock |
|---|---|---|
| `test_record_started_inserts_row` | Row present with `status='running'` after call | Real SQLite (`:memory:` via `tmp_path`) |
| `test_record_started_idempotent` | Double call → 1 row | Real SQLite |
| `test_record_completed_transitions` | `status` becomes `completed`, `completed_at` set | Real SQLite |
| `test_record_failed_transitions` | `status` becomes `failed`, `error_msg` set | Real SQLite |
| `test_record_completed_idempotent` | Already-terminal row unchanged | Real SQLite |
| `test_pending_runs_empty` | Returns `[]` after all terminal | Real SQLite |
| `test_pending_runs_after_crash` | Returns `running` row when not transitioned | Real SQLite |

**`tests/unit/test_council.py`**

| Test | Description | Mock |
|---|---|---|
| `test_run_council_empty_text` | Returns `[]`; provider never called | `FakeProvider` (assert not called) |
| `test_run_council_writes_session_file` | 10-turn conversation → `{date}-session-*.md` written | `FakeProvider`, `tmp_path` |
| `test_run_council_writes_category_files` | Response has all sections → 4 files written | `FakeProvider`, `tmp_path` |
| `test_run_council_skips_empty_section` | Response missing `## open-questions` → that file not written | `FakeProvider`, `tmp_path` |
| `test_run_council_merges_existing` | Existing `user-preferences.md` content passed in prompt; new content includes both old + new facts | `FakeProvider`, `tmp_path` |
| `test_run_council_no_duplicate_on_second_call` | Calling council twice with same fact → single bullet in file | `FakeProvider`, `tmp_path` |
| `test_run_council_existing_untouched_on_missing_section` | LLM response omits `## key-facts` → existing `key-facts.md` on disk unchanged | `FakeProvider`, `tmp_path` |
| `test_run_council_provider_error` | Provider raises → returns `[]`; no crash; no files written | `FakeProvider` raises |
| `test_load_existing_categories_missing_dir` | Returns `{"user-preferences": "", ...}` when memory dir absent | `tmp_path` |
| `test_parse_council_sections_all_headers` | Full response parsed into correct 4-key dict | No mock |
| `test_parse_council_sections_missing_header` | Missing header key returns `""` not `KeyError` | No mock |

**`tests/unit/test_council_timer.py`** (new)

| Test | Description | Mock |
|---|---|---|
| `test_timer_fires_after_idle` | After `idle_seconds` with no new turns, council called once | `asyncio` time mock, `FakeProvider` |
| `test_timer_resets_on_new_turn` | Second turn cancels first timer; council fires only after second timer expires | `asyncio` time mock, `FakeProvider` |
| `test_timer_multiple_sessions_independent` | Two sessions have independent timers; each fires at correct time | `asyncio` time mock, `FakeProvider` |
| `test_flush_on_shutdown_runs_council` | `_flush_council_on_shutdown()` with 2 pending timers → council runs for both | `FakeProvider`, `tmp_path` |
| `test_flush_on_shutdown_empty` | `_flush_council_on_shutdown()` with no pending timers → no-op | No mock |
| `test_disabled_council_no_timer` | `council.enabled: false` → `_council_timers` stays empty | No mock |

### Integration Testing

**`tests/integration/test_pipeline.py`** (new)

| Scenario | Components | Expected outcome |
|---|---|---|
| Parent spawns researcher via registry | `SubagentRegistry.resolve()` → `spawn_subagent()` → `researcher.py` | Parent yields `SubagentStarted`, `TurnComplete` from child, `SubagentCompleted` |
| Parent spawns ad-hoc script | `spawn_subagent("tests/fixtures/echo_agent.py", ...)` | Same event sequence |
| Crash recovery: `pending_runs()` | `record_started()` → kill process → `pending_runs()` | Returns the interrupted row |
| Council merges with existing memory | `run_council()` called twice; second call's prompt contains first call's output | Category files contain deduplicated content |
| Council fires once per session, not per turn | 3 turns in quick succession with `idle_seconds=1`; assert council called exactly once | `FakeProvider`, `asyncio.sleep` mock |
| Registry enriches system prompt | `SubagentRegistry.to_prompt_block()` → `AgentLoop` system prompt | Prompt contains "Available Subagents" table |

### E2E / Acceptance

| Acceptance criterion | Verification method |
|---|---|
| Spawning `researcher.py` → parent yields `SubagentCompleted` | `test_pipeline.py` integration test |
| After simulated process kill, `pending_runs()` returns interrupted run | `test_durable_runs.py` — insert `running` row, assert returned |
| `run_council()` writes merged category files; calling twice with same fact produces one bullet | `test_council.py` `test_run_council_no_duplicate_on_second_call` |
| `test_cold_start.py` still passes with all new modules present | CI gate — import time < 200ms |
| `ruff check src/` and `mypy --strict src/` clean on all new modules | Pre-commit hook |

---

## Next Steps

- **Phase 1C:** Scratch dir cleanup policy (`cleanup_old_runs` default + CLI hook), subagent stderr capture and log routing, council provider selection vs main loop provider, `SubagentRegistry.validate()` at startup (warn vs raise decision), `SIGTERM` graceful drain of in-flight `asyncio.create_task` council jobs, ruff/mypy notes.
