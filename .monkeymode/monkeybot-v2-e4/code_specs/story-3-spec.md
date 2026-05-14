# Code Spec: Story 3 — Durable Run Store

**Story:** E4 Story 3 — user_stories.md  
**Design Reference:** 1a-discovery.md ADR-002, 1b-contracts.md `core/durable_runs.py`  
**Date:** 2026-05-13

## Implementation Summary

- **Files to Create:** 2 files
- **Files to Modify:** 0 files
- **Tests to Add:** 1 test file
- **Estimated Complexity:** S

## Codebase Conventions

Same as Story 1: `from __future__ import annotations`, PEP 8 imports, `asyncio_mode = "auto"`, `tmp_path`, `mypy --strict`, `ruff check`.

## Technical Context

**Key Gotchas:**
- Pattern is a direct mirror of `ConversationHistory` in `src/monkeybot/core/history.py` — open-per-call with `async with aiosqlite.connect(self._db_path)`, WAL mode set in `init()`, `Path.mkdir(parents=True, exist_ok=True)` in `init()`
- `record_completed` and `record_failed` use `UPDATE ... WHERE run_id=? AND status='running'` — this is the idempotency mechanism; no extra SELECT needed
- `started_at` / `completed_at` are Unix milliseconds — use `int(time.time() * 1000)` (same as `history.py`)
- `INSERT OR IGNORE` for `record_started` — no raise on duplicate
- `pending_runs()` returns plain dicts (not dataclasses) — use `aiosqlite` row-as-dict via `db.row_factory = aiosqlite.Row` + `dict(row)`

**Reusable Utilities:**
- `src/monkeybot/core/history.py` — direct pattern reference for `__init__`, `init()`, and per-call connection pattern
- `aiosqlite` — already in deps
- `ulid` — already in deps (used by callers for `run_id`, not by this module)

**Integration Points:**
- Phase 6: `DurableRunStore` instantiated in `_serve_async` and `_run_async`; `init()` called at startup; `record_started/completed/failed` called from `spawn_subagent` caller in `_dispatch_tool`

---

## Task Breakdown

### Task 1: `core/durable_runs.py`

**Dependencies:** None  
**Files**: `src/monkeybot/core/durable_runs.py` (create)  
**Pattern:** Mirror `src/monkeybot/core/history.py` exactly for class structure and connection handling.

**SQL Definitions (module-level constants):**

```python
_CREATE_TABLE = """
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
)"""

_CREATE_IDX_STATUS = """
CREATE INDEX IF NOT EXISTS idx_durable_runs_status ON durable_runs(status)
"""

_CREATE_IDX_PARENT = """
CREATE INDEX IF NOT EXISTS idx_durable_runs_parent ON durable_runs(parent_run_id)
"""
```

**Signatures:**

```python
class DurableRunStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path  # bare file path, no "sqlite:///" prefix

    async def init(self) -> None:
        """Create table and indexes. WAL + NORMAL. Safe to call multiple times."""

    async def record_started(
        self,
        run_id: str,
        script: str,
        scratch_dir: str,
        parent_run_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """INSERT OR IGNORE row with status='running' and started_at=now_ms."""

    async def record_completed(self, run_id: str) -> None:
        """UPDATE SET status='completed', completed_at=now_ms WHERE run_id=? AND status='running'."""

    async def record_failed(self, run_id: str, error_msg: str) -> None:
        """UPDATE SET status='failed', error_msg=?, completed_at=now_ms WHERE run_id=? AND status='running'."""

    async def pending_runs(self) -> list[dict[str, Any]]:
        """SELECT all rows WHERE status='running'. Returns list of dicts."""
```

**`init()` implementation:**
1. `Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)`
2. Open connection, set `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`
3. Execute `_CREATE_TABLE`, `_CREATE_IDX_STATUS`, `_CREATE_IDX_PARENT`
4. `await db.commit()`

**`pending_runs()` implementation:**
```python
async with aiosqlite.connect(self._db_path) as db:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT run_id, agent_name, script, scratch_dir, parent_run_id, started_at "
        "FROM durable_runs WHERE status='running'"
    ) as cur:
        rows = await cur.fetchall()
return [dict(row) for row in rows]
```

---

### Task 2: Unit Tests

**Dependencies:** Task 1  
**Files**: `tests/unit/test_durable_runs.py` (create)

**Pattern:** Follow `tests/unit/test_history.py` — async fixture with `tmp_path`, `asyncio_mode = "auto"`.

```python
import pytest
from monkeybot.core.durable_runs import DurableRunStore

@pytest.fixture
async def store(tmp_path):
    s = DurableRunStore(str(tmp_path / "test.db"))
    await s.init()
    return s
```

**Test Cases:**

- `test_record_started_inserts_row(store)`: call `record_started("r1", "x.py", "/tmp/x")`; query DB directly via `aiosqlite`; assert `status='running'`, `completed_at IS NULL`
- `test_record_started_idempotent(store)`: call twice with same `run_id`; assert exactly 1 row
- `test_record_completed_transitions(store)`: `record_started` + `record_completed`; assert `status='completed'`, `completed_at IS NOT NULL`
- `test_record_failed_transitions(store)`: `record_started` + `record_failed("r1", "timeout")`; assert `status='failed'`, `error_msg='timeout'`, `completed_at` set
- `test_record_completed_idempotent(store)`: `record_started` + `record_completed` + `record_failed` (in that order); assert status is still `'completed'` (terminal state preserved)
- `test_pending_runs_returns_running(store)`: insert 1 running + 1 completed row; `pending_runs()` returns list with exactly 1 dict, `run_id` matches running row
- `test_pending_runs_empty(store)`: all rows completed; `pending_runs()` returns `[]`
- `test_init_creates_db_file(tmp_path)`: construct with nested path `tmp_path / "a/b/test.db"`; call `init()`; assert file exists
- `test_init_idempotent(tmp_path)`: call `init()` twice on same path; assert no error and data survives

**Direct DB assertion pattern** (used in `test_record_started_inserts_row`):
```python
import aiosqlite
async with aiosqlite.connect(str(tmp_path / "test.db")) as db:
    async with db.execute("SELECT status, completed_at FROM durable_runs WHERE run_id='r1'") as cur:
        row = await cur.fetchone()
assert row is not None
assert row[0] == "running"
assert row[1] is None
```

---

## Final Verification

**Functionality:**
- [ ] `record_started` inserts `status='running'` row
- [ ] Double `record_started` with same `run_id` → 1 row (INSERT OR IGNORE)
- [ ] `record_completed` transitions `status` to `'completed'` with `completed_at` set
- [ ] `record_failed` transitions `status` to `'failed'` with `error_msg` and `completed_at` set
- [ ] `record_failed` on already-completed row → no change (idempotent)
- [ ] `pending_runs()` returns only `status='running'` rows as plain dicts
- [ ] `init()` creates file and parent directories if missing
- [ ] `init()` is idempotent (safe to call multiple times)

**Code Quality:**
- [ ] `ruff check src/monkeybot/core/durable_runs.py` passes
- [ ] `mypy --strict src/monkeybot/core/durable_runs.py` passes
- [ ] WAL mode confirmed in at least one test

**Testing:**
- [ ] All 9 test cases pass
- [ ] Tests use real SQLite via `tmp_path` (no mocking)
