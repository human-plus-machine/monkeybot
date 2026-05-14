# Code Spec: Story 3 — Tools Layer

**Story:** user_stories.md "Story 3: Tools Layer"  
**Design Reference:** 1b-contracts.md "tools/ — Five Tool Functions", 1c-operations.md "Security Design — Input Validation"  
**Date:** 2026-05-13  
**Complexity:** S

## Implementation Summary
- **Files to Create:** 5 (4 source + 1 test)
- **Files to Modify:** 0
- **Estimated LOC:** ~250 source, ~150 test

## Codebase Conventions

- All tool functions are **synchronous** except `run_command` (async via `asyncio.create_subprocess_shell`)
- Each module exports a `TOOL_DEF` dict (or `TOOL_DEFS` list for file_ops) — JSON Schema for the model
- Error strings returned to model start with `"ERROR: "` — never raise in tool functions
- Sync tools are called via `asyncio.to_thread` in the loop — do not make them async

---

## Task 1: `tools/run_command.py`

**Files:** `src/monkeybot/tools/run_command.py` (create)  
**Deps:** `asyncio`, `os`, `time` (stdlib only)

Implement verbatim from `monkeybot_v2_plan.md` Section 6 "run_command.py". Key points:

- `CommandResult` dataclass: `stdout`, `stderr`, `exit_code`, `duration_ms`
- `TOOL_DEF` dict with JSON Schema (copy from plan — `command` required, `working_dir` + `timeout` optional)
- `async def run_command(command, working_dir=None, timeout=30) -> CommandResult`
- `asyncio.TimeoutError` → `CommandResult(stdout="", stderr=f"Command timed out after {timeout}s", exit_code=124, duration_ms=timeout*1000)`

**Add `format_result(r: CommandResult) -> str`** (not in plan, needed by loop):
```python
def format_result(r: CommandResult) -> str:
    parts = [f"exit_code: {r.exit_code}", f"stdout: {r.stdout}"]
    if r.stderr:
        parts.append(f"stderr: {r.stderr}")
    parts.append(f"duration_ms: {r.duration_ms}")
    return "\n".join(parts)
```

**Security — `_safe_env()`** (from 1c-operations.md):
```python
_REDACTED_VARS = {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DB_URL"}

def _safe_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _REDACTED_VARS:
        if key in env:
            env[key] = "REDACTED"
    return env
```
Pass `env=_safe_env()` to `asyncio.create_subprocess_shell`.

---

## Task 2: `tools/file_ops.py`

**Files:** `src/monkeybot/tools/file_ops.py` (create)  
**Deps:** `pathlib` (stdlib)

Implement verbatim from plan Section 6 "file_ops.py". Add `allowed_roots` security parameter:

```python
from pathlib import Path

def read_file(path: str, *, allowed_roots: list[Path] | None = None) -> str:
    resolved = Path(path).resolve()
    if allowed_roots is not None:
        if not any(str(resolved).startswith(str(r.resolve())) for r in allowed_roots):
            return f"ERROR: Access denied: {path}"
    if not resolved.exists():
        return f"ERROR: File not found: {path}"
    return resolved.read_text()

def write_file(path: str, content: str, append: bool = False,
               *, allowed_roots: list[Path] | None = None) -> str:
    resolved = Path(path).resolve()
    if allowed_roots is not None:
        if not any(str(resolved).startswith(str(r.resolve())) for r in allowed_roots):
            return f"ERROR: Access denied: {path}"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    resolved.open(mode).write(content)
    return f"OK: wrote {len(content)} chars to {path}"
```

`TOOL_DEFS` is a list with two entries (copy from plan).

---

## Task 3: `tools/memory_ops.py`

**Files:** `src/monkeybot/tools/memory_ops.py` (create)  
**Deps:** stdlib only (inline implementation to avoid circular import before Story 2 merges)

**Implementation:** Copy the `search_memory` function body from `core/memory.py` (Story 2) inline. When both stories merge, `memory_ops.py` can `from monkeybot.core.memory import search_memory` and delegate — but the inline copy is safe because they're identical.

`TOOL_DEF` dict: copy from plan Section 6 "memory_ops.py".

The tool function signature matches the tool def:
```python
def search_memory(query: str, memory_path: str, max_results: int = 5) -> str: ...
```

---

## Task 4: `tools/skill_ops.py`

**Files:** `src/monkeybot/tools/skill_ops.py` (create)  
**Deps:** `pathlib` (stdlib)

Implement verbatim from plan Section 6 "skill_ops.py". No changes needed.

`TOOL_DEF` dict: copy from plan.

---

## Task 5: Tests

**Files:** `tests/unit/test_tools.py` (create)  
**Pattern:** Use `tmp_path` for all filesystem operations; `pytest.mark.asyncio` (auto mode) for `run_command`.

```python
import pytest
from pathlib import Path
from monkeybot.tools.run_command import run_command, format_result
from monkeybot.tools.file_ops import read_file, write_file
from monkeybot.tools.skill_ops import list_skills
from monkeybot.tools.memory_ops import search_memory

async def test_run_command_echo():
    result = await run_command("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout

async def test_run_command_timeout():
    result = await run_command("sleep 100", timeout=1)
    assert result.exit_code == 124
    assert "timed out" in result.stderr

def test_read_file_exists(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    assert read_file(str(f)) == "hello"

def test_read_file_missing(tmp_path):
    result = read_file(str(tmp_path / "nope.txt"))
    assert result.startswith("ERROR: File not found")

def test_read_file_access_denied(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("secret")
    other = tmp_path / "other"
    other.mkdir()
    result = read_file(str(f), allowed_roots=[other])
    assert result.startswith("ERROR: Access denied")
```

Additional test cases (follow pattern above):
- `write_file` creates file + content
- `write_file` append mode
- `write_file` access denied with `allowed_roots`
- `write_file` creates parent dirs automatically
- `list_skills` with 2 skill dirs → returns both names
- `list_skills` with `filter` matching one → returns only that one
- `list_skills` on empty/missing dir → returns `"No skills found."`
- `search_memory` with matching files → returns ranked excerpts
- `format_result` with stderr present → includes stderr line; without → omits it
- `_safe_env()` redacts `GEMINI_API_KEY` (set via monkeypatch, check dict doesn't contain real value)

---

## Final Verification

**Functionality:**
- [ ] `run_command("echo hello")` → `exit_code=0`, stdout contains "hello"
- [ ] Timeout → `exit_code=124`, stderr contains "timed out"
- [ ] `read_file` on missing path → starts with `"ERROR: File not found"`
- [ ] `allowed_roots` blocks access outside roots
- [ ] `write_file` creates parent directories
- [ ] `list_skills` filter works
- [ ] `_safe_env()` redacts known secret env vars

**Code Quality:**
- [ ] All 4 tool modules export correct `TOOL_DEF` / `TOOL_DEFS` with valid JSON Schema
- [ ] `ruff check` and `mypy --strict` pass
- [ ] No module-level I/O or heavy imports

**Tests:**
- [ ] `pytest tests/unit/test_tools.py` passes
- [ ] No tests use real filesystem paths outside `tmp_path`
