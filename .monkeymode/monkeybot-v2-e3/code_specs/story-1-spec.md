# Code Spec: Story 1 — Usage Recording & CLI

**Story:** `.monkeymode/monkeybot-v2-e3/user_stories.md` — Story 1  
**Design Reference:** `1a-discovery.md` §Core Data Model, `1b-contracts.md` §core/usage.py  
**Date:** 2026-05-13

## Implementation Summary

- **Files to Create:** 2 (`core/usage.py`, `tests/unit/test_usage.py`)
- **Files to Modify:** 1 (`cli.py` — add `usage` command + wire `record_usage` into `_run_async` / `_serve_async`)
- **Estimated Complexity:** S

## Codebase Conventions

**Patterns:** `src/monkeybot/core/history.py` — reference implementation for all aiosqlite patterns  
**Imports:** stdlib → third-party → local; `from __future__ import annotations` at top  
**Types:** `mypy --strict`; use `from __future__ import annotations` to defer evaluation  
**Testing:** `pytest` + `pytest-asyncio`; `asyncio_mode = "auto"` in `pyproject.toml`; use `:memory:` SQLite for unit tests  
**CLI:** `@main.command()` + `@click.option(...)` pattern — see `cli.py` existing `run` and `serve` commands

---

## Task 1: Create `core/usage.py`

**Files**: `src/monkeybot/core/usage.py` (create)  
**Pattern**: Follow `src/monkeybot/core/history.py` exactly for aiosqlite open-per-call, WAL pragma, lazy init

**Key types:**

```python
from __future__ import annotations
import time
from dataclasses import dataclass
import aiosqlite
import ulid
from monkeybot.core.events import TurnComplete

@dataclass
class UsageSummary:
    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_cost_usd: float
    avg_latency_ms: float
    since_hours: float

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS turn_usage (
    id            TEXT    PRIMARY KEY,
    run_id        TEXT    NOT NULL UNIQUE,
    session_id    TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
)"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_turn_usage_created ON turn_usage(created_at)"

async def record_usage(db_path: str, session_id: str, event: TurnComplete) -> None: ...
async def get_usage_summary(db_path: str, since_hours: float) -> UsageSummary: ...
```

**`record_usage` algorithm:**
1. `Path(db_path).parent.mkdir(parents=True, exist_ok=True)`
2. `async with aiosqlite.connect(db_path) as db:` — set WAL + synchronous=NORMAL (same two pragmas as history.py)
3. `CREATE TABLE IF NOT EXISTS` + index
4. `INSERT OR IGNORE INTO turn_usage (id, run_id, session_id, ...) VALUES (?, ?, ?, ...)` — `id=str(ulid.new())`, `created_at=int(time.time()*1000)`
5. `await db.commit()`
6. Add comment: `# NOTE: cost_usd is 0.0 until providers implement cost models`

**`get_usage_summary` algorithm:**
1. Same lazy init (table + index)
2. `since_ms = int((time.time() - since_hours * 3600) * 1000)`
3. `SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cached_tokens), SUM(cost_usd), AVG(duration_ms) FROM turn_usage WHERE created_at >= ?`
4. If row is all `None` (no data): return `UsageSummary(turns=0, ...)` with all zeros
5. Return `UsageSummary` with query results; `avg_latency_ms` = `row[5] or 0.0`; `since_hours` = passed-in arg

**logger:** `log = logging.getLogger("monkeybot.usage")`; log DEBUG on successful record, ERROR on exception (re-raise after logging)

---

## Task 2: Add `usage` command to `cli.py`

**Files**: `src/monkeybot/cli.py` (modify — 3 changes)  
**Pattern**: Follow existing `@main.command()` blocks

**Change 1 — add import at top of file:**
```python
from monkeybot.core.usage import UsageSummary, get_usage_summary, record_usage
```

**Change 2 — add `usage` command after `serve` command:**

```python
@main.command()
@click.option("--since", default=24.0, type=float, help="Look back N hours (default: 24)")
def usage(since: float) -> None:
    """Show token usage and cost summary."""
    db_url = os.getenv("DB_URL", "sqlite:///data/monkeybot.db")
    db_path = db_url.removeprefix("sqlite:///")
    summary = asyncio.run(get_usage_summary(db_path, since))
    if summary.turns == 0:
        click.echo("No usage data found.")
        return
    click.echo(f"Usage summary (last {since:.0f}h)")
    click.echo("─" * 36)
    click.echo(f"{'Turns':<20}: {summary.turns:>10,}")
    click.echo(f"{'Input tokens':<20}: {summary.input_tokens:>10,}")
    click.echo(f"{'Output tokens':<20}: {summary.output_tokens:>10,}")
    click.echo(f"{'Cached tokens':<20}: {summary.cached_tokens:>10,}")
    click.echo(f"{'Total cost (USD)':<20}: ${summary.total_cost_usd:>10.4f}")
    click.echo(f"{'Avg latency (ms)':<20}: {summary.avg_latency_ms:>10.0f}")
```

**Change 3 — wire `record_usage` into `_run_async` and `_serve_async`:**

In both functions, the event stream is currently consumed directly by `CLIGateway` and `WebhookGateway` (the loop's `run()` generator is passed to the gateway, not iterated in `_run_async`/`_serve_async` directly). 

**Correct approach:** The gateways iterate the loop internally. We need to wrap the loop so usage is recorded. Add a helper coroutine:

```python
async def _collect_usage(
    loop: AgentLoop,
    user_message: str,
    session_id: str,
    db_path: str,
    user_id: str | None = None,
) -> None:
    """Iterate loop events and record usage on TurnComplete."""
    async for event in loop.run(user_message, session_id, user_id):
        if isinstance(event, TurnComplete):
            try:
                await record_usage(db_path, session_id, event)
            except Exception:
                logging.getLogger(__name__).exception("Failed to record usage run_id=%s", event.run_id)
```

**Wait** — re-reading `cli.py` and `gateway/cli.py`: `CLIGateway.run_interactive()` calls `self._loop.run(...)` internally. Usage must be recorded at the gateway level.

**Simpler approach:** In `_run_async`, after `await gateway.run_interactive()` is called, the gateway already consumed all events. Instead, pass `db_path` to `CLIGateway` and have it call `record_usage` when it sees `TurnComplete`. 

**Simplest approach that doesn't touch gateway internals:** Subclass is overkill. Instead, add an optional `on_turn_complete` callback to `AgentLoop.__init__` that is `await`-ed when `TurnComplete` is emitted in `loop.run()`.

**Actually — read loop.py again:** `loop.run()` is an async generator. The gateway iterates it. The cleanest change with zero gateway modification:

In `loop.py`'s `finally` block, `TurnComplete` is yielded. The _caller_ of `loop.run()` (the gateway) processes all events. Since we want to avoid modifying gateways, we inject an optional coroutine callback:

```python
# In AgentLoop.__init__:
self._on_turn_complete: Callable[[TurnComplete], Awaitable[None]] | None = None

# In loop.run() finally block, after yielding TurnComplete:
if self._on_turn_complete is not None:
    try:
        await self._on_turn_complete(tc_event)
    except Exception:
        log.exception("on_turn_complete callback failed")
```

In `_run_async` and `_serve_async`, after building `agent_loop`:
```python
db_path = db_url.removeprefix("sqlite:///")
agent_loop._on_turn_complete = lambda e: record_usage(db_path, session_id_for_usage, e)
```

**Problem:** `session_id` is per-message for webhook (not known at construction time).

**Final correct approach:** The cleanest seam without touching existing gateway internals is to add a **single optional `on_turn_complete` callback** to `AgentLoop.__init__`. The loop calls it in the `finally` block. `cli.py` sets it after constructing the loop with a closure over `db_path`. The `session_id` is already available inside `loop.run()` — pass it to the callback:

```python
# loop.py - new optional arg
from collections.abc import Callable, Awaitable
from typing import Any

class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        history: ConversationHistory,
        inspectors: list[ToolInspector],
        config: dict[str, Any],
        on_turn_complete: Callable[[str, TurnComplete], Awaitable[None]] | None = None,
    ) -> None:
        ...
        self._on_turn_complete = on_turn_complete

    async def run(self, user_message, session_id, user_id=None):
        ...
        finally:
            tc = TurnComplete(...)
            yield tc
            if self._on_turn_complete is not None:
                try:
                    await self._on_turn_complete(session_id, tc)
                except Exception:
                    log.exception("on_turn_complete callback failed run_id=%s", run_id)
```

In `cli.py`:
```python
db_path = db_url.removeprefix("sqlite:///")
agent_loop = AgentLoop(
    ...,
    on_turn_complete=lambda sid, ev: record_usage(db_path, sid, ev),
)
```

This is the correct approach. It requires modifying `loop.py` as well.

**Files to modify:**
- `src/monkeybot/cli.py`
- `src/monkeybot/core/loop.py` (add `on_turn_complete` optional callback — additive, no breaking change)

---

## Task 3: Write `tests/unit/test_usage.py`

**Files**: `tests/unit/test_usage.py` (create)  
**Pattern**: Follow `tests/unit/test_history.py`

```python
import pytest, time
from monkeybot.core.usage import record_usage, get_usage_summary, UsageSummary
from monkeybot.core.events import TurnComplete

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")

def make_event(run_id="RUN1", input_tokens=100, output_tokens=50, duration_ms=200):
    return TurnComplete(run_id=run_id, input_tokens=input_tokens,
                        output_tokens=output_tokens, duration_ms=duration_ms)
```

**Test cases:**
- `test_record_inserts_row`: call `record_usage`, then `get_usage_summary(since_hours=1)` → `summary.turns == 1`, `summary.input_tokens == 100`
- `test_record_idempotent`: call `record_usage` twice with same `run_id` → `summary.turns == 1`
- `test_summary_empty_returns_zeros`: empty DB → `UsageSummary(turns=0, input_tokens=0, ...)`
- `test_summary_aggregates_correctly`: insert 3 rows (100+200+300 input tokens) → `summary.input_tokens == 600`
- `test_summary_since_filter`: insert 2 rows with `created_at` in window, 1 row older (mock `time.time` or insert directly with old timestamp via raw aiosqlite) → `summary.turns == 2`

**For `test_summary_since_filter`:** Insert an old row directly using aiosqlite with `created_at = int((time.time() - 48*3600)*1000)`, then call `get_usage_summary(since_hours=24)`.

---

## Final Verification

- [ ] `record_usage` inserts exactly one row per unique `run_id`
- [ ] `INSERT OR IGNORE` prevents duplicates (no exception on repeat call)
- [ ] `get_usage_summary` returns all-zeros when no rows match
- [ ] `since_hours` filter correctly excludes old rows
- [ ] `usage` CLI command prints formatted output or "No usage data found."
- [ ] `on_turn_complete` callback wired in both `_run_async` and `_serve_async`
- [ ] `loop.py` change is additive (default `None`, no existing call sites broken)
- [ ] `ruff check src/` and `mypy --strict src/` clean
- [ ] All 5 unit tests pass
