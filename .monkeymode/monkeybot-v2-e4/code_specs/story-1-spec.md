# Code Spec: Story 1 — Subagent Protocol & Scratch Dirs

**Story:** E4 Story 1 — user_stories.md  
**Design Reference:** 1a-discovery.md ADR-001, 1b-contracts.md `core/subagent_proto.py`, `core/runs.py`  
**Date:** 2026-05-13

## Implementation Summary

- **Files to Create:** 5 files
- **Files to Modify:** 0 files
- **Tests to Add:** 2 test files
- **Estimated Complexity:** M

## Codebase Conventions

- `from __future__ import annotations` at top of every module
- Import order: stdlib → third-party → local (`monkeybot.*`)
- `asyncio_mode = "auto"` — all test functions are `async def`, no decorator needed
- `tmp_path` fixture for filesystem tests; real subprocess for integration-style unit tests
- Logging: `logging.getLogger("monkeybot.<module>")` — log at WARNING for errors, DEBUG for raw data
- Type checking: `mypy --strict`; `ruff check` pre-commit

## Technical Context

**Key Gotchas:**
- `spawn_subagent` must be an `AsyncGenerator` (use `async def` + `yield`) — callers iterate with `async for`
- Child stdout is **exclusively** for `AgentEvent` JSON lines — child must use stderr for debug
- `asyncio.wait_for` timeout is reset per-line, not per-process (per-line read timeout)
- Never use `shell=True` in `asyncio.create_subprocess_exec`
- `SubagentDefinition` is defined here (not in `subagent_registry.py`) so Story 2 has a single import target: `from monkeybot.core.subagent_proto import SubagentDefinition`

**Reusable Utilities:**
- `event_to_json`, `event_from_json`, `AgentEvent` — `monkeybot.core.events` (already exists)
- `SubagentStarted`, `SubagentCompleted`, `TurnComplete`, `ErrorEvent` — already in `events.py`
- `ulid.new()` — ULID generation (already in deps)

**Integration Points:**
- Story 2 imports `SubagentDefinition` from this module
- Phase 6 wires `spawn_subagent` as a tool call handler in `AgentLoop._dispatch_tool`
- `create_scratch_dir` called inside `spawn_subagent` before subprocess spawn

---

## Task Breakdown

### Task 1: `core/runs.py` — Scratch Dir Helpers

**Dependencies:** None  
**Files**: `src/monkeybot/core/runs.py` (create)  
**Pattern**: Stateless stdlib functions — no class, no state

**Implementation:**
1. `create_scratch_dir(run_id, base_dir=None)` — builds path `{base_dir or tempfile.gettempdir()}/monkeybot-run-{run_id}`, calls `os.makedirs(path, mode=0o700, exist_ok=True)`, returns absolute path string
2. `cleanup_old_runs(base_dir, max_age_days=7)` — globs `monkeybot-run-*` under `base_dir`, computes age via `os.path.getmtime`, calls `shutil.rmtree` on stale dirs with `ignore_errors=True`, returns count deleted

**Test Cases** (in `tests/unit/test_subagent_proto.py`, see Task 4):
- `create_scratch_dir(run_id)` → dir exists, mode `0o700`, path contains run_id
- `create_scratch_dir(run_id, base_dir=tmp_path)` → dir created under `tmp_path`
- `cleanup_old_runs`: 3 dirs, 2 old → returns 2, only old ones deleted (set mtime via `os.utime`)

---

### Task 2: `core/subagent_proto.py` — Data Types & Child-Side Helpers

**Dependencies:** Task 1 (imports `create_scratch_dir`)

**Files to Create:**

| File | Purpose |
|------|---------|
| `src/monkeybot/core/subagent_proto.py` | All types + spawn function + child-side helpers |

**Key Signatures:**

```python
from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass
from typing import Any
from collections.abc import AsyncGenerator

from monkeybot.core.events import AgentEvent, event_from_json, event_to_json


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    script: str
    description: str
    skills_path: str
    model: str
    timeout_seconds: int


@dataclass
class SubagentEnvelope:
    run_id: str
    parent_run_id: str | None
    agent_name: str | None
    task: str
    context: dict[str, Any]
    skills_path: str
    model: str
    scratch_dir: str


def read_envelope_from_stdin() -> SubagentEnvelope:
    """Read and deserialize one JSON line from sys.stdin.
    Raises ValueError if stdin is empty or JSON is malformed."""


def emit_event(event: AgentEvent) -> None:
    """Write event as JSON line to sys.stdout and flush."""


async def spawn_subagent(
    definition_or_script: SubagentDefinition | str,
    task: str,
    context: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    timeout_seconds: int = 300,
) -> AsyncGenerator[AgentEvent, None]:
    ...
```

**Implementation Algorithm:**

`read_envelope_from_stdin`:
1. `line = sys.stdin.readline()` — raises `ValueError` if empty
2. `data = json.loads(line)` — raises `ValueError` on malformed JSON
3. Return `SubagentEnvelope(**data)`

`emit_event`:
1. `sys.stdout.write(event_to_json(event) + "\n")`
2. `sys.stdout.flush()`

`spawn_subagent`:
1. Resolve `script` and effective `timeout_seconds` from `definition_or_script` (if `SubagentDefinition`, use `.script` and `.timeout_seconds`; if `str`, use as-is + the `timeout_seconds` param)
2. Generate `run_id = str(ulid.new())`
3. Call `create_scratch_dir(run_id)` → `scratch_dir`
4. Resolve `agent_name = definition_or_script.name if isinstance(definition_or_script, SubagentDefinition) else None`
5. Build `SubagentEnvelope` with resolved values
6. `proc = await asyncio.create_subprocess_exec(sys.executable, script, stdin=PIPE, stdout=PIPE, stderr=PIPE)` — wrap in `try/except FileNotFoundError` → `yield ErrorEvent("Script not found: {script}", recoverable=False); return`
7. Write `json.dumps(dataclasses.asdict(envelope)) + "\n"` to `proc.stdin`; `await proc.stdin.drain(); proc.stdin.close()`
8. `yield SubagentStarted(run_id=run_id, script=script, parent_run_id=parent_run_id)`
9. Read stdout in a loop with per-line `asyncio.wait_for(proc.stdout.readline(), timeout=effective_timeout)`:
   - Empty bytes → break (EOF)
   - Decode → try `event_from_json(line)` → `yield event`; if `TurnComplete` → break
   - `ValueError`/`json.JSONDecodeError` → `yield ErrorEvent(recoverable=True)`; log raw line at WARNING
   - `asyncio.TimeoutError` → terminate proc; `yield ErrorEvent("Subagent timeout", recoverable=True)`; break
10. Collect stderr lines (from `proc.stderr.read()`) → log each at DEBUG
11. `await proc.wait()`; if `proc.returncode != 0` and no `TurnComplete` was yielded → `yield ErrorEvent(f"Subagent exited with code {proc.returncode}", recoverable=True)`
12. `yield SubagentCompleted(run_id=run_id, scratch_dir=scratch_dir)`

**Critical Notes:**
- Track whether `TurnComplete` was yielded (flag) to know if we need the non-zero-exit `ErrorEvent`
- `asyncio.create_subprocess_exec` takes positional args — `sys.executable` is the first arg, `script` is the second
- stderr must be read after the stdout loop ends (or with `asyncio.gather`) — don't deadlock on stderr buffer

---

### Task 3: `subagents/researcher.py` — Example Subagent Script

**Dependencies:** Tasks 1 & 2  
**Files**: `subagents/researcher.py` (create)  
**Pattern**: Standalone script — not imported; runs as `python subagents/researcher.py`

**Implementation:**
```python
"""Research subagent — reads SubagentEnvelope from stdin, runs a minimal research loop."""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, "src")

from monkeybot.core.subagent_proto import emit_event, read_envelope_from_stdin
from monkeybot.core.events import AssistantDelta, TurnComplete
import ulid

def main() -> None:
    envelope = read_envelope_from_stdin()
    run_id = envelope.run_id
    try:
        # Minimal stub: emit a delta and complete
        emit_event(AssistantDelta(text=f"Researching: {envelope.task}"))
        emit_event(TurnComplete(run_id=run_id))
    except Exception:
        from monkeybot.core.events import ErrorEvent
        traceback.print_exc(file=sys.stderr)
        emit_event(ErrorEvent(message="researcher error", recoverable=False))
        sys.exit(1)

main()
```

**Note:** This is a working end-to-end validator, not a full research tool. The real research logic (web search, skills loading) is out of scope for Story 1.

---

### Task 4: `tests/fixtures/echo_agent.py` — Minimal Subprocess Fixture

**Dependencies:** Tasks 1 & 2  
**Files**: `tests/fixtures/echo_agent.py` (create)

```python
"""Minimal subagent for testing: reads envelope, emits TurnComplete, exits 0."""
from __future__ import annotations
import sys
sys.path.insert(0, "src")
from monkeybot.core.subagent_proto import read_envelope_from_stdin, emit_event
from monkeybot.core.events import TurnComplete

envelope = read_envelope_from_stdin()
emit_event(TurnComplete(run_id=envelope.run_id))
```

---

### Task 5: Unit Tests

**Dependencies:** Tasks 1–4  
**Files**: `tests/unit/test_subagent_proto.py` (create)

**Pattern:** Follow `tests/unit/test_history.py` — `asyncio_mode = "auto"`, `async def test_*`, `tmp_path` fixture, real subprocess calls (no mocking of subprocess).

**Test Cases:**

```python
# Envelope round-trip
async def test_envelope_json_roundtrip():
    env = SubagentEnvelope(run_id="r1", parent_run_id=None, agent_name="x",
                           task="do thing", context={}, skills_path="/s",
                           model="gemini", scratch_dir="/tmp/x")
    data = dataclasses.asdict(env)
    env2 = SubagentEnvelope(**data)
    assert env == env2
```

Remaining tests (concise form — follow the pattern above):
- `test_spawn_echo_script`: spawn `tests/fixtures/echo_agent.py`; collect events; assert order is `SubagentStarted`, `TurnComplete`, `SubagentCompleted`
- `test_spawn_malformed_line`: fixture script that writes `"garbage\n"` then exits; assert `ErrorEvent(recoverable=True)` in collected events
- `test_spawn_timeout`: fixture script that calls `time.sleep(999)`; spawn with `timeout_seconds=1`; assert `ErrorEvent` with `"timeout"` in message
- `test_spawn_nonzero_exit`: fixture script that exits with code 1 without emitting `TurnComplete`; assert `ErrorEvent` then `SubagentCompleted`
- `test_emit_event_writes_json_line`: patch `sys.stdout` with `io.StringIO`; call `emit_event(TurnComplete())`; assert exactly one non-empty line in output
- `test_read_envelope_from_stdin`: patch `sys.stdin` with `io.StringIO` containing valid JSON; assert returned `SubagentEnvelope` matches

Scratch dir tests (can be in the same file or a short block at end):
- `test_create_scratch_dir_mode`: dir created with `0o700`, path contains `run_id`
- `test_cleanup_old_runs_returns_count`: create 3 dirs, backdate 2 via `os.utime`, call `cleanup_old_runs`, assert returns 2

**Fixture scripts for tests:** inline `tmp_path`-based scripts using `subprocess` setup — or simply reference `tests/fixtures/echo_agent.py` for the happy path. For error cases, write minimal inline scripts to `tmp_path` and spawn those.

---

## Reference Code Example

**Async generator pattern** (to ensure mypy accepts `spawn_subagent`):

```python
from collections.abc import AsyncGenerator

async def spawn_subagent(...) -> AsyncGenerator[AgentEvent, None]:
    yield SubagentStarted(...)
    # ... loop ...
    yield SubagentCompleted(...)
```

This is the correct type annotation for an async generator function. Do NOT annotate as `AsyncIterator` — mypy strict distinguishes them.

---

## Final Verification

**Functionality:**
- [ ] `spawn_subagent(echo_agent)` yields `SubagentStarted → TurnComplete → SubagentCompleted` in order
- [ ] Malformed stdout line → `ErrorEvent(recoverable=True)`, loop continues
- [ ] Timeout → subprocess terminated, `ErrorEvent(message includes "timeout")`
- [ ] Non-zero exit without `TurnComplete` → `ErrorEvent` then `SubagentCompleted`
- [ ] `create_scratch_dir` creates dir with mode `0o700`
- [ ] `cleanup_old_runs` returns correct count, skips current dirs

**Code Quality:**
- [ ] `ruff check src/monkeybot/core/subagent_proto.py src/monkeybot/core/runs.py` passes
- [ ] `mypy --strict src/monkeybot/core/subagent_proto.py src/monkeybot/core/runs.py` passes
- [ ] stdout used exclusively for events; no `print()` in subagent scripts

**Testing:**
- [ ] All 7+ test cases pass
- [ ] Tests use real subprocess (not mocked) for end-to-end coverage
