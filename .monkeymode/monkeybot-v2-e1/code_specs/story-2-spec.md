# Code Spec: Story 2 — Persistence Layer

**Story:** user_stories.md "Story 2: Persistence Layer"  
**Design Reference:** 1a-discovery.md "Message", 1b-contracts.md "core/history.py" and "core/memory.py", 1c-operations.md "SQLite WAL Mode"  
**Date:** 2026-05-13  
**Complexity:** S

## Implementation Summary
- **Files to Create:** 4 (2 source + 2 test)
- **Files to Modify:** 0
- **Estimated LOC:** ~200 source, ~150 test

## Codebase Conventions

Same as Story 1. Key additions for this story:
- **Async:** All DB methods are `async`; `aiosqlite` via `async with aiosqlite.connect(path) as db`
- **ULID IDs:** `import ulid; str(ulid.new())` — verify this returns a `str` in the installed version
- **Timestamps:** Unix milliseconds — `int(time.time() * 1000)`
- **Test isolation:** Every test uses `tmp_path` (pytest fixture) — no shared state

---

## Task 1: `core/history.py`

**Files:** `src/monkeybot/core/history.py` (create), `tests/unit/test_history.py` (create)  
**Deps:** `aiosqlite`, `ulid-py`

**Note on `Message` import:** `Message` lives in `core/provider.py` (Story 1). Import it: `from monkeybot.core.provider import Message`. If implementing this story before Story 1 is merged, use the inline stub from user_stories.md "Story 2 Integration Contracts" for tests only — remove the stub once Story 1 is merged.

```python
from __future__ import annotations
import time
import aiosqlite
import ulid
from pathlib import Path
from monkeybot.core.provider import Message  # from Story 1

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    tool_call_id TEXT,
    tool_name   TEXT,
    created_at  INTEGER NOT NULL
)"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at)
"""

class ConversationHistory:
    def __init__(self, db_url: str = "sqlite:///data/monkeybot.db") -> None:
        # Parse "sqlite:///path" → extract path
        # Store as self._db_path: str
        ...

    async def init(self) -> None:
        # Create parent dirs, connect, PRAGMA journal_mode=WAL,
        # PRAGMA synchronous=NORMAL, CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS
        ...

    async def save(self, session_id: str, role: str, content: str,
                   tool_call_id: str | None = None,
                   tool_name: str | None = None) -> None:
        # INSERT with fresh ULID + current timestamp
        ...

    async def load(self, session_id: str) -> list[Message]:
        # SELECT WHERE session_id=? ORDER BY created_at ASC
        # Map rows to Message objects
        ...

    async def clear(self, session_id: str) -> None:
        # DELETE WHERE session_id=?
        ...
```

**`db_url` parsing:** Strip the `sqlite:///` prefix to get the file path. Handle both `sqlite:///relative/path.db` (relative) and `sqlite:////abs/path.db` (absolute). Simplest approach: `path = db_url.removeprefix("sqlite:///")`.

**`init()` — create parent dirs:** `Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)`.

**`load()` — row to Message mapping:**
```python
rows = await cur.fetchall()
return [
    Message(role=row[0], content=row[1], tool_call_id=row[2], tool_name=row[3])
    for row in rows
]
```
(SELECT role, content, tool_call_id, tool_name WHERE session_id=? ORDER BY created_at ASC)

**Test cases (`tests/unit/test_history.py`):**

```python
import pytest
from monkeybot.core.history import ConversationHistory

@pytest.fixture
async def history(tmp_path):
    h = ConversationHistory(db_url=f"sqlite:///{tmp_path}/test.db")
    await h.init()
    return h

async def test_save_and_load(history):
    await history.save("s1", "user", "Hello")
    await history.save("s1", "assistant", "Hi there")
    msgs = await history.load("s1")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"

async def test_load_empty_session(history):
    assert await history.load("nonexistent") == []

async def test_order_is_ascending(history):
    await history.save("s1", "user", "first")
    await history.save("s1", "assistant", "second")
    msgs = await history.load("s1")
    assert msgs[0].content == "first"
    assert msgs[1].content == "second"
```

Additional test cases (follow pattern above):
- `init()` called twice on same DB → no error, data preserved
- Process restart simulation: create history, save, create NEW history instance on same path, `init()`, `load()` → same messages
- `tool_call_id` and `tool_name` round-trip correctly
- WAL mode check: after `init()`, `PRAGMA journal_mode` returns `"wal"`

---

## Task 2: `core/memory.py`

**Files:** `src/monkeybot/core/memory.py` (create), add tests to `tests/unit/test_memory.py` (create)  
**Deps:** stdlib only (`pathlib`)

```python
from __future__ import annotations
from pathlib import Path

def save_memory(memory_path: str, filename: str, content: str) -> str:
    """Write {memory_path}/{filename}.md. Creates dirs if needed."""
    p = Path(memory_path) / f"{filename}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK: saved memory/{filename}.md"

def search_memory(query: str, memory_path: str, max_results: int = 5) -> str:
    """
    Keyword search over *.md files. Score = count of query words found (case-insensitive).
    Returns formatted excerpts (first 500 chars per file), sorted by score desc.
    """
    p = Path(memory_path)
    if not p.exists():
        return "No memory files found."

    keywords = [k.lower() for k in query.split() if k]
    results: list[tuple[int, Path, str]] = []

    for f in sorted(p.glob("**/*.md")):
        try:
            content = f.read_text()
        except OSError:
            continue
        score = sum(1 for k in keywords if k in content.lower())
        if score > 0:
            results.append((score, f, content))

    if not results:
        return f"No memory files matched: {query}"

    results.sort(key=lambda x: x[0], reverse=True)
    output = []
    for score, f, content in results[:max_results]:
        preview = content[:500].strip()
        output.append(f"### {f.stem}\n{preview}\n...")
    return "\n\n".join(output)
```

**Test cases (`tests/unit/test_memory.py`):**
- `save_memory` creates file at `{memory_path}/{filename}.md` with correct content
- `save_memory` creates parent dirs if they don't exist
- `search_memory` with 5 files, 3 matching → returns 3 excerpts, highest score first
- `search_memory` with no matches → returns sentinel string containing query text
- `search_memory` with non-existent `memory_path` → returns `"No memory files found."`
- `search_memory` respects `max_results=2` limit
- `search_memory` is case-insensitive (query "Python" matches file with "python")

---

## Final Verification

**Functionality:**
- [ ] Save 3 messages → `load()` returns all 3 in ascending order
- [ ] `init()` is idempotent (no error on existing DB)
- [ ] Process-restart persistence (new instance, same path, data survives)
- [ ] WAL mode enabled after `init()`
- [ ] `search_memory` scoring and ranking correct
- [ ] `save_memory` creates dirs and file

**Code Quality:**
- [ ] `ruff check` and `mypy --strict` pass on both files
- [ ] No sync file I/O in async methods (history uses `aiosqlite` throughout)
- [ ] `ulid.new()` returns a `str` — confirm and add comment if cast needed

**Tests:**
- [ ] All tests use `tmp_path` — no global state, no leftover files
- [ ] `pytest tests/unit/test_history.py tests/unit/test_memory.py` passes
