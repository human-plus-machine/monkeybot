# Code Spec: Story 2 — Safety Inspector Factory

**Story:** User Story 2 — Safety Inspector Factory  
**Design Reference:** 1A ADR-E2-001, 1B `core/safety.py`  
**Date:** 2026-05-13  

## Implementation Summary

- **Files to Create:** 2 files
- **Files to Modify:** 0 files
- **Estimated Complexity:** S

## Codebase Conventions

Same as project-wide (see Story 1 spec). Pattern to follow: `src/monkeybot/core/inspector.py` — `from __future__ import annotations`, type annotations, module docstring.

## Technical Context

**Key Gotchas:**
- Do NOT modify `cli.py` in this story — that is Story 4's responsibility.
- The function body is intentionally trivial; the real value is in edge-case test coverage.
- Treat any malformed YAML value (`null`, wrong type) as absent — no exceptions raised.
- `CommandTierInspector` takes a plain `dict` (the `command_tiers` sub-dict, not the full `safety` dict).
- `RulesInspector` takes `denied_patterns: list[str]`.

**Reusable Utilities (from E1):**
- `monkeybot.core.inspector`: `CommandTierInspector`, `RulesInspector`, `ToolInspector`

## Task Breakdown

### Task 1: Create `core/safety.py`

**Dependencies:** None  
**Files:** `src/monkeybot/core/safety.py` (create)

**Full implementation** (the body is already specified in the user story — tests are the deliverable):

```python
"""Safety inspector factory — builds inspector chain from config.yaml dict."""
from __future__ import annotations

from typing import Any

from monkeybot.core.inspector import CommandTierInspector, RulesInspector, ToolInspector


def load_inspectors(config: dict[str, Any]) -> list[ToolInspector]:
    """Build inspector chain from parsed config.yaml dict.

    Args:
        config: Full bot config dict (or {} for dev mode).

    Returns:
        Ordered list of ToolInspector instances. Empty list = allow all.
        Order: [CommandTierInspector, RulesInspector] when both present.

    Raises:
        Nothing. Missing/malformed keys are treated as absent.
    """
    safety = config.get("safety") or {}
    if not isinstance(safety, dict):
        return []
    inspectors: list[ToolInspector] = []
    tiers = safety.get("command_tiers")
    if isinstance(tiers, dict):
        inspectors.append(CommandTierInspector(tiers))
    patterns = safety.get("denied_patterns")
    if isinstance(patterns, list):
        inspectors.append(RulesInspector(denied_patterns=patterns))
    return inspectors
```

---

### Task 2: Unit tests for `load_inspectors`

**Dependencies:** Task 1  
**Files:** `tests/unit/test_safety.py` (create)

**Pattern:** Follow `tests/unit/test_loop.py` — `from __future__ import annotations`, module docstring, `pytest`, no fixtures needed (pure function).

**Test cases — factory behavior:**
- `config={}` → returns `[]`
- `config={"other_key": 1}` (no `safety` key) → returns `[]`
- `config={"safety": {"command_tiers": {...}}}` only → returns `[CommandTierInspector]`
- `config={"safety": {"denied_patterns": [...]}}` only → returns `[RulesInspector]`
- Both present → returns `[CommandTierInspector, RulesInspector]` in that order
- `config={"safety": None}` → returns `[]` (null safety key)
- `config={"safety": {"command_tiers": None}}` → returns `[]` (null tiers)
- `config={"safety": {"denied_patterns": None}}` → returns `[]` (null patterns)

**Test cases — inspector behavior (verify E1 contracts via factory output):**

All these tests call `load_inspectors()` to build the inspector, then call its `check()` method:

- Tool in `denied` tier → `Decision(kind="deny")`
- Tool in `pre_approved` tier → `Decision(kind="allow")`
- Tool in `requires_approval` tier → `Decision(kind="approve")`
- Tool not in any tier → `Decision(kind="allow")` (default)
- Args containing a `denied_patterns` substring → `Decision(kind="deny")`
- Args not matching any pattern → `Decision(kind="allow")`

**Helper for async checks:**
```python
import asyncio
from monkeybot.core.provider import ToolCall

def check_sync(inspector, tool_name, args=None):
    call = ToolCall(call_id="test", name=tool_name, args=args or {})
    return asyncio.run(inspector.check(call, ctx=None))  # type: ignore[arg-type]
```

## Final Verification

**Functionality:**
- [ ] All 8 factory behavior test cases pass
- [ ] All 6 inspector behavior test cases pass
- [ ] `cli.py` is NOT modified (defer to Story 4)

**Code Quality:**
- [ ] `ruff check` clean
- [ ] `mypy --strict` clean
- [ ] No exceptions raised for any malformed config input
