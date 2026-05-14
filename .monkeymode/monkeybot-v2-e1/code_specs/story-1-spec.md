# Code Spec: Story 1 — Core Types & Protocols

**Story:** user_stories.md "Story 1: Core Types & Protocols"  
**Design Reference:** 1a-discovery.md "Core Data Model", 1b-contracts.md "core/events.py", "core/provider.py", "core/inspector.py"  
**Date:** 2026-05-13  
**Complexity:** S

## Implementation Summary
- **Files to Create:** 6 (4 source + 2 test)
- **Files to Modify:** 0
- **Estimated LOC:** ~350 source, ~150 test

## Codebase Conventions

- **Naming:** `snake_case` modules, `PascalCase` classes, `snake_case` functions
- **Imports:** `from __future__ import annotations` first, then stdlib, then local
- **Types:** All public functions fully annotated; `mypy --strict` must pass
- **Testing:** `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` in pyproject.toml)
- **Dataclasses:** Use `@dataclass` (not Pydantic) for events and value objects
- **Protocols:** Use `@runtime_checkable` so `isinstance()` works without inheritance

## Implementation Order (within story)

Implement in this order — each file depends on the prior:
1. `context.py` (no deps)
2. `events.py` (no deps)
3. `provider.py` (imports `TurnContext` from `context.py`)
4. `inspector.py` (imports `ToolCall` from `provider.py`, `TurnContext` from `context.py`)

---

## Task 1: `core/context.py`

**Files:** `src/monkeybot/core/context.py` (create), `tests/unit/test_context.py` (create)  
**Deps:** None

**Key types:**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

@dataclass
class SkillRef:
    name: str           # directory name under skills_path
    description: str    # first non-heading line of SKILL.md, max 120 chars
    path: str           # absolute path to SKILL.md

@dataclass(frozen=True)
class TurnContext:
    agent_md: str
    memory_index: list[str]     # "{stem}: {first_line}" per .md file
    skills: list[SkillRef]
    user_id: str | None = None
    parent_run_id: str | None = None
    run_id: str | None = None

    def build_system_prompt(self) -> str: ...

def load_turn_context(
    agent_md_path: str,
    memory_path: str,
    skills_path: str,
    user_id: str | None = None,
    parent_run_id: str | None = None,
    run_id: str | None = None,
) -> TurnContext: ...
```

**`build_system_prompt()` algorithm:**
1. Start with `self.agent_md`
2. If `memory_index`: append `"\n## Memory Index\n" + "\n".join(f"- {l}" for l in self.memory_index)`
3. If `skills`: append skills list + usage hint (see 1b-contracts.md exact text)
4. `"\n".join(parts)`

**`load_turn_context()` algorithm:**
1. `agent_md = Path(agent_md_path).read_text()` — raises `FileNotFoundError` if missing
2. `memory_index = _build_memory_index(memory_path)` — returns `[]` if path missing
3. `skills = _scan_skills(skills_path)` — returns `[]` if path missing

**`_build_memory_index(memory_path)`:** `sorted(Path(memory_path).glob("**/*.md"))` → for each file, read first line, strip `#`, format as `"{stem}: {line}"`. Swallow `OSError`/`IndexError`.

**`_scan_skills(skills_path)`:** `sorted(Path(skills_path).glob("*/SKILL.md"))` → for each, `name = skill_md.parent.name`, description = first non-empty non-heading line ≤ 120 chars.

**Test cases (`tests/unit/test_context.py`):**
- `load_turn_context` with all three dirs populated → correct counts in memory_index and skills
- Missing `memory_path` → `memory_index == []`, no exception
- Missing `skills_path` → `skills == []`, no exception  
- Missing `agent_md_path` → raises `FileNotFoundError`
- `build_system_prompt()` with memory + skills → string contains `"## Memory Index"` and `"## Available Skills"`
- `build_system_prompt()` with empty memory + empty skills → returns just `agent_md` content

---

## Task 2: `core/events.py`

**Files:** `src/monkeybot/core/events.py` (create), `tests/unit/test_events.py` (create)  
**Deps:** None

**All 10 dataclasses + union type + two helpers.** Reference the exact definitions from `monkeybot_v2_plan.md` Section 4 "src/monkeybot/core/events.py" — implement verbatim, then add the `event_from_json` mapping dict.

**`event_to_json`:** `json.dumps(dataclasses.asdict(event))`

**`event_from_json`:** parse `data["kind"]` → look up in `mapping` dict → `cls(**{k: v for k, v in data.items() if k != "kind"})`. Raise `ValueError` if kind not in mapping.

**Critical:** `timestamp` in `UserMessage` defaults to `int(time.time() * 1000)` — milliseconds, not seconds.

**Test cases (`tests/unit/test_events.py`):**

```python
# Reference pattern — all 10 types follow this:
import dataclasses, json
from monkeybot.core.events import (
    UserMessage, AssistantDelta, ToolCallStarted, ToolCallResult,
    ApprovalRequest, ApprovalResponse, SubagentStarted, SubagentCompleted,
    TurnComplete, ErrorEvent, event_to_json, event_from_json,
)

def test_user_message_roundtrip():
    e = UserMessage(content="hello", user_id="u1")
    assert event_from_json(event_to_json(e)) == e

def test_turn_complete_roundtrip():
    e = TurnComplete(run_id="r1", input_tokens=10, output_tokens=5, cost_usd=0.001, duration_ms=500)
    assert event_from_json(event_to_json(e)) == e

def test_unknown_kind_raises():
    import pytest
    with pytest.raises(ValueError, match="Unknown event kind"):
        event_from_json('{"kind": "bogus"}')
```

Add one test per remaining event type following the same pattern.

---

## Task 3: `core/provider.py`

**Files:** `src/monkeybot/core/provider.py` (create)  
**Deps:** `context.py` for `TurnContext` type

**Implement verbatim from `monkeybot_v2_plan.md` Section 4 "src/monkeybot/core/provider.py":**
- `Message`, `ToolDef`, `TextDelta`, `ToolCall`, `ProviderUsage`, `ProviderDone` dataclasses
- `ProviderEvent = TextDelta | ToolCall | ProviderDone`
- `Provider` Protocol with `@runtime_checkable`

**No test file for provider.py** — the Protocol contract is tested indirectly via `FakeProvider` in Story 6's loop tests. Add one smoke test in `test_context.py`:

```python
def test_fake_provider_satisfies_protocol():
    from monkeybot.core.provider import Provider
    class FakeProvider:
        name = "fake"
        supports_streaming = True
        async def stream(self, messages, tools, *, model, system, context=None):
            async def _gen(): yield  # type: ignore
            return _gen()
    assert isinstance(FakeProvider(), Provider)
```

---

## Task 4: `core/inspector.py`

**Files:** `src/monkeybot/core/inspector.py` (create)  
**Deps:** `provider.py` (`ToolCall`), `context.py` (`TurnContext`)

**Implement verbatim from `monkeybot_v2_plan.md` Section 4 "src/monkeybot/core/inspector.py":**
- `Decision` dataclass
- `ToolInspector` Protocol (`@runtime_checkable`)
- `CommandTierInspector` class
- `RulesInspector` class

**Test cases** (add to `tests/unit/test_context.py` or new `tests/unit/test_inspector.py`):
- `CommandTierInspector({"denied": ["rm_all"]}).check(ToolCall("c1","rm_all",{}), ctx)` → `Decision(kind="deny")`
- `CommandTierInspector({"requires_approval": ["deploy"]}).check(ToolCall("c1","deploy",{}), ctx)` → `Decision(kind="approve")`
- `CommandTierInspector({}).check(ToolCall("c1","echo",{}), ctx)` → `Decision(kind="allow")`
- `RulesInspector(["sudo"]).check(ToolCall("c1","run_command",{"command":"sudo rm"}), ctx)` → `Decision(kind="deny")`
- `RulesInspector(["sudo"]).check(ToolCall("c1","run_command",{"command":"ls"}), ctx)` → `Decision(kind="allow")`

Use a simple `TurnContext` stub for `ctx` (pass `agent_md=""`, `memory_index=[]`, `skills=[]`).

---

## Final Verification

**Functionality:**
- [ ] All 10 event types serialize/deserialize without loss
- [ ] `event_from_json(event_to_json(e)) == e` for all 10 types
- [ ] `isinstance(FakeProvider(), Provider)` is `True`
- [ ] `load_turn_context` raises `FileNotFoundError` for missing `agent_md_path`
- [ ] `load_turn_context` returns empty lists for missing `memory_path`/`skills_path`
- [ ] All 3 inspector decisions work correctly

**Code Quality:**
- [ ] `ruff check src/monkeybot/core/events.py src/monkeybot/core/context.py src/monkeybot/core/provider.py src/monkeybot/core/inspector.py` — zero warnings
- [ ] `mypy --strict` on same 4 files — zero errors
- [ ] `from __future__ import annotations` at top of each file
- [ ] No module-level I/O (no file reads at import time)

**Tests:**
- [ ] `pytest tests/unit/test_events.py tests/unit/test_context.py` passes
- [ ] All 10 event round-trip tests pass
- [ ] All inspector decision tests pass
