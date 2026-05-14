# Code Spec: Story 4 — LLM Council & Session Memory

**Story:** E4 Story 4 — user_stories.md  
**Design Reference:** 1a-discovery.md ADR-003, 1b-contracts.md `core/council.py`, `cli.py` timer wiring  
**Date:** 2026-05-13

## Implementation Summary

- **Files to Create:** 3 files
- **Files to Modify:** 2 files
- **Tests to Add:** 2 test files
- **Estimated Complexity:** M

## Codebase Conventions

Same as Stories 1–3: `from __future__ import annotations`, PEP 8 imports, `asyncio_mode = "auto"`, `tmp_path`, `mypy --strict`, `ruff check`, JSON structured logging via `logging.getLogger("monkeybot.council")`.

## Technical Context

**Key Gotchas:**
- `run_council()` **never raises** — all exceptions are caught and logged; this is a fire-and-forget function called from `asyncio.create_task()`
- `_parse_council_sections` splits on `"\n## "` — note the leading newline, which means the first section header at position 0 is handled separately (strip, then split `"\n## "`)
- The `asyncio.create_task()` GC risk: the task must be stored in `_background_tasks` set with a `done_callback` that discards it — otherwise the GC may silently cancel the task
- `_council_timers` key is `session_id` (string) — per-session debounce, not global
- `_flush_council_on_shutdown` must be called in the `finally` block of `_serve_async` — it replaces the scheduled sleep with an immediate run
- `council.enabled` defaults to `False` — must be explicitly set to `true` in config; absence of the key = disabled

**Reusable Utilities:**
- `save_memory(memory_path, filename, content)` — `monkeybot.core.memory` (already exists); writes `{memory_path}/{filename}.md`
- `Provider` Protocol — `monkeybot.core.provider`; `provider.stream(messages, tools, model=model)` returns async iterator of chunks
- `history.load(session_id)` — `ConversationHistory.load()` already exists
- `src/monkeybot/core/scheduler.py` — asyncio task lifecycle pattern (cancel + await)

**Integration Points:**
- `cli.py` `_serve_async` / `_run_async`: construct council state, wire `_on_turn_complete`, add shutdown flush
- Phase 6: `AgentLoop` passes `on_turn_complete=_on_turn_complete` — this callback is the entry point for the council
- `FakeProvider` needed in tests — create a minimal stub in the test file itself (no shared fixture)

---

## Task Breakdown

### Task 1: `core/council.py` — Constants and Core Function

**Dependencies:** None (pure Python + E1 deps)  
**Files**: `src/monkeybot/core/council.py` (create)

**Signatures:**

```python
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from monkeybot.core.provider import Provider

from monkeybot.core.memory import save_memory

log = logging.getLogger("monkeybot.council")

MANAGED_CATEGORIES: tuple[str, ...] = (
    "user-preferences",
    "key-facts",
    "open-questions",
)

COUNCIL_PROMPT: str = """\
You are the LLM Memory Council. Your job is to maintain the agent's long-term memory.
...
"""  # Full template from 1b-contracts.md


async def run_council(
    conversation_text: str,
    memory_path: str,
    provider: Provider,
    model: str,
    session_id: str,
) -> list[str]:
    """Read existing categories → call LLM → write merged files.
    Returns list of filenames written. Never raises."""


def _load_existing_categories(memory_path: str) -> dict[str, str]:
    """Read each MANAGED_CATEGORIES file. Returns {name: content or ""}. Never raises."""


def _parse_council_sections(response: str) -> dict[str, str]:
    """Split LLM response on '## ' headers. Returns {slug: content}."""
```

**`COUNCIL_PROMPT` — copy verbatim from `1b-contracts.md` section "COUNCIL_PROMPT".**

**`_load_existing_categories` implementation:**
```python
result: dict[str, str] = {}
for cat in MANAGED_CATEGORIES:
    path = Path(memory_path) / f"{cat}.md"
    try:
        result[cat] = path.read_text() if path.exists() else ""
    except OSError:
        log.warning("council: could not read %s", path)
        result[cat] = ""
return result
```

**`_parse_council_sections` implementation:**
1. Strip response
2. Split on `"\n## "` to get raw sections — first chunk is pre-header preamble (discard)
3. For each chunk: first line is the header, rest is content
4. Slug = `header.lower().strip().replace(" ", "-")`
5. Content = rest of chunk stripped
6. Return `{slug: content}` — never raises

**`run_council` implementation:**
1. If `conversation_text.strip()` is empty → return `[]`
2. `existing = _load_existing_categories(memory_path)`
3. Build `existing_memories_block`: for each category, format as `"### {cat}\n{content or '(no existing memories)'}\n"`
4. Format `COUNCIL_PROMPT` with `existing_memories_block` and `conversation_text`
5. Call provider — collect full response text:
   ```python
   from monkeybot.core.provider import Message
   chunks: list[str] = []
   async for chunk in provider.stream([Message(role="user", content=prompt)], [], model=model):
       if hasattr(chunk, "text"):
           chunks.append(chunk.text)
   response = "".join(chunks)
   ```
6. `sections = _parse_council_sections(response)`
7. Write files:
   - Session file: filename = `f"{date.today()}-session-{session_id[:8]}"` → `save_memory(memory_path, filename, sections.get("summary", ""))`
   - For each category in `MANAGED_CATEGORIES`: if `sections.get(cat)` is non-empty → `save_memory(memory_path, cat, sections[cat])`
8. Collect filenames written; return them
9. Wrap entire try/except: `except Exception: log.error("council error", exc_info=True); return []`

**Critical Notes:**
- The try/except wraps the entire function body (step 5 onwards) — early return for empty text is outside the try
- `save_memory` takes `filename` without `.md` extension — it appends `.md` internally
- Provider streaming: look at how existing tests use provider — the `FakeProvider` pattern should mimic whatever `provider.stream()` yields

---

### Task 2: Tests for `core/council.py`

**Dependencies:** Task 1  
**Files**: `tests/unit/test_council.py` (create)

**`FakeProvider` stub** (define in test file):
```python
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any

@dataclass
class _Chunk:
    text: str

class FakeProvider:
    def __init__(self, response: str = "") -> None:
        self._response = response
        self.called = False
        self.last_messages: list[Any] = []

    async def stream(self, messages, tools, *, model=""):  # type: ignore[override]
        self.called = True
        self.last_messages = messages
        yield _Chunk(text=self._response)
```

**Full response string for tests** — a constant with all 4 sections:
```python
FULL_RESPONSE = """\
## Summary
This session covered testing.

## user-preferences
- Prefers concise answers

## key-facts
- Python 3.11 in use

## open-questions
- What is the timeout?
"""
```

**Test Cases:**
- `test_run_council_empty_text(tmp_path)`: `await run_council("", ...)` → returns `[]`, `provider.called` is `False`
- `test_run_council_writes_session_file(tmp_path)`: call with `FakeProvider(FULL_RESPONSE)` → session file exists under `tmp_path`
- `test_run_council_writes_all_category_files(tmp_path)`: all 3 category `.md` files exist after call; return value contains 4 filenames
- `test_run_council_skips_empty_section(tmp_path)`: response missing `## open-questions` → `open-questions.md` not written; existing `open-questions.md` on disk unchanged
- `test_run_council_merges_existing(tmp_path)`: pre-write `user-preferences.md` with content `"- old fact"`; fake provider returns `"## user-preferences\n- old fact\n- new fact"`; assert file contains both
- `test_run_council_provider_error(tmp_path)`: `FakeProvider` raises `RuntimeError` → returns `[]`, no crash, no files written
- `test_load_existing_categories_missing_dir(tmp_path)`: call with non-existent `memory_path` → all values are `""`
- `test_parse_council_sections_all_headers()`: parse `FULL_RESPONSE` → dict has keys `"summary"`, `"user-preferences"`, `"key-facts"`, `"open-questions"`
- `test_parse_council_sections_missing_header()`: parse response without `## key-facts` → `sections.get("key-facts", "")` returns `""`, no KeyError
- `test_run_council_does_not_raise_on_save_error(tmp_path, monkeypatch)`: monkeypatch `save_memory` to raise `OSError`; `run_council(...)` returns a list (possibly partial), no crash

---

### Task 3: `cli.py` — Council Timer Wiring

**Dependencies:** Task 1  
**Files**: `src/monkeybot/cli.py` (modify)

**Scope:** Add council idle timer state and callbacks. Do NOT change any existing CLI logic — additive only.

**Additions at module level** (after existing imports):
```python
# Council idle timer state — module-level so _on_turn_complete and _flush can share them
_council_timers: dict[str, asyncio.Task[None]] = {}
_background_tasks: set[asyncio.Task[None]] = set()
```

**New imports to add:**
```python
from monkeybot.core.council import run_council
```
(Import lazily inside the function if needed to avoid circular imports.)

**New async functions** (add after `_load_inspectors`):

```python
async def _on_turn_complete(
    session_id: str,
    tc: object,  # TurnComplete — typed loosely to avoid circular import
    *,
    config: dict[str, object],
    history: ConversationHistory,
    provider: object,
    memory_path: str,
) -> None:
    council_cfg = config.get("council", {})
    if not isinstance(council_cfg, dict) or not council_cfg.get("enabled"):
        return

    existing = _council_timers.pop(session_id, None)
    if existing and not existing.done():
        existing.cancel()

    idle_seconds = int(council_cfg.get("idle_seconds", 300))
    bot_model = str(config.get("model", "gemini-2.0-flash"))
    council_model = str(council_cfg.get("model", bot_model))

    async def _fire() -> None:
        await asyncio.sleep(idle_seconds)
        _council_timers.pop(session_id, None)
        msgs = await history.load(session_id)
        text = "\n".join(f"{m.role}: {m.content}" for m in msgs)
        await run_council(text, memory_path, provider, council_model, session_id)  # type: ignore[arg-type]

    task = asyncio.create_task(_fire())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    _council_timers[session_id] = task


async def _flush_council_on_shutdown(
    *,
    history: ConversationHistory,
    provider: object,
    memory_path: str,
    council_model: str,
) -> None:
    """Cancel all pending idle timers; run council immediately for each session."""
    for session_id, task in list(_council_timers.items()):
        task.cancel()
        try:
            msgs = await history.load(session_id)
            text = "\n".join(f"{m.role}: {m.content}" for m in msgs)
            await run_council(text, memory_path, provider, council_model, session_id)  # type: ignore[arg-type]
        except Exception:
            logging.getLogger(__name__).exception("flush_council failed session=%s", session_id)
    _council_timers.clear()
```

**Wire into `_serve_async` and `_run_async`:**

In both functions, replace the `on_turn_complete=lambda sid, ev: record_usage(...)` with a combined callback:
```python
memory_path = str(config["memory_path"])
council_cfg = bot_config.get("council", {})
bot_model = str(config["model"])
council_model = str(council_cfg.get("model", bot_model)) if isinstance(council_cfg, dict) else bot_model

async def _turn_cb(sid: str, ev: object) -> None:
    await asyncio.gather(
        record_usage(db_path, sid, ev),  # type: ignore[arg-type]
        _on_turn_complete(sid, ev, config=config, history=history,
                          provider=provider, memory_path=memory_path),
    )

agent_loop = AgentLoop(
    ...
    on_turn_complete=_turn_cb,
)
```

In `_serve_async`, add to the `finally` block:
```python
finally:
    if scheduler is not None:
        await scheduler.stop()
    await _flush_council_on_shutdown(
        history=history, provider=provider,
        memory_path=memory_path, council_model=council_model,
    )
```

**Note:** `_run_async` (interactive mode) doesn't need `_flush_council_on_shutdown` — the user exits cleanly and the process ends; the council idle timer will fire if the user waited long enough, otherwise the session memory for this run is accepted as lost (ADR-003).

---

### Task 4: Council Timer Tests

**Dependencies:** Tasks 1 & 3  
**Files**: `tests/unit/test_council_timer.py` (create)

**Approach:** Test `_on_turn_complete` and `_flush_council_on_shutdown` in isolation — import them directly, inject `FakeProvider`, use `asyncio` time-mocking or very short `idle_seconds` (e.g. 0.05s) to avoid real waits in CI.

**`FakeHistory` stub** (define in test file):
```python
class FakeHistory:
    def __init__(self, messages=None):
        self._messages = messages or []
    async def load(self, session_id: str):
        return self._messages
```

**`FakeRunCouncil` monkeypatch approach:** monkeypatch `monkeybot.cli.run_council` with a coroutine that records calls.

**Test Cases:**
- `test_timer_fires_after_idle(monkeypatch, tmp_path)`: call `_on_turn_complete` with `idle_seconds=0.05`; `await asyncio.sleep(0.1)`; assert `run_council` was called once
- `test_timer_resets_on_new_turn(monkeypatch, tmp_path)`: call `_on_turn_complete` twice quickly; sleep past first timeout but before second; assert `run_council` called 0 times; sleep past second; assert called exactly once
- `test_flush_on_shutdown_runs_council(monkeypatch, tmp_path)`: add 2 sessions to `_council_timers`; call `_flush_council_on_shutdown`; assert `run_council` called twice; `_council_timers` is empty after
- `test_flush_on_shutdown_empty(monkeypatch)`: `_council_timers` is empty; call `_flush_council_on_shutdown`; no error; `run_council` not called
- `test_disabled_council_no_timer(monkeypatch)`: config with `council.enabled: false`; call `_on_turn_complete`; `_council_timers` remains empty
- `test_council_enabled_missing_key(monkeypatch)`: config with no `council` key; `_on_turn_complete` returns without creating timer

**Critical Note:** Each test must clear `_council_timers` and `_background_tasks` before and after to avoid state bleed between tests. Use a `@pytest.fixture` or monkeypatch to reset.

---

### Task 5: Update `bots/example-bot/config.yaml`

**Dependencies:** Task 1  
**Files**: `bots/example-bot/config.yaml` (modify — add `council:` block only)

**Change:** Append a commented `council:` block. Do not touch the `subagents:` block from Story 2 or any existing keys.

```yaml
# --- LLM Council (E4) ---
# council:
#   enabled: false          # set to true to enable session memory consolidation
#   idle_seconds: 300       # seconds of inactivity before council fires; min 60
#   model: "gemini-2.0-flash"  # optional; falls back to model.default
```

---

## Final Verification

**Functionality:**
- [ ] `run_council("")` returns `[]` without calling provider
- [ ] Full council run writes 4 files: session + 3 category files
- [ ] Missing LLM section → existing file on disk untouched
- [ ] Provider error → `run_council` returns `[]`, no crash
- [ ] `_on_turn_complete` with `council.enabled: false` creates no timer
- [ ] Debounce: 3 rapid turns → 1 council call after idle
- [ ] `_flush_council_on_shutdown` cancels timers and fires council for each pending session
- [ ] `asyncio.create_task` result stored in `_background_tasks` to prevent GC

**Code Quality:**
- [ ] `ruff check src/monkeybot/core/council.py src/monkeybot/cli.py` passes
- [ ] `mypy --strict src/monkeybot/core/council.py` passes
- [ ] `run_council` never raises — all paths covered by try/except

**Testing:**
- [ ] All 10 council unit tests pass
- [ ] All 6 timer tests pass
- [ ] Tests clean up `_council_timers` between runs (no state bleed)
