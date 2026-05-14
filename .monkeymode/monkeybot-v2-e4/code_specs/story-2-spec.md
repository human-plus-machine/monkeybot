# Code Spec: Story 2 — Subagent Registry

**Story:** E4 Story 2 — user_stories.md  
**Design Reference:** 1a-discovery.md ADR-005, 1b-contracts.md `core/subagent_registry.py`  
**Date:** 2026-05-13

## Implementation Summary

- **Files to Create:** 2 files
- **Files to Modify:** 1 file
- **Tests to Add:** 1 test file
- **Estimated Complexity:** S

## Codebase Conventions

Same as Story 1: `from __future__ import annotations`, PEP 8 import order, `asyncio_mode = "auto"`, `tmp_path` fixture, `mypy --strict`, `ruff check`.

## Technical Context

**Key Gotchas:**
- `SubagentDefinition` is imported from `monkeybot.core.subagent_proto` (Story 1). During parallel dev, stub it with a local frozen dataclass — replace with the real import once Story 1 is committed.
- Name validation regex `^[a-z0-9][a-z0-9-]*$` — raise `ValueError` at `__init__` time (fail-fast), not at `resolve()` time
- `all_definitions()` returns a **copy** of the internal list — callers must not mutate the registry's backing store
- `validate()` resolves script paths relative to `Path.cwd()` — document this clearly in docstring

**Reusable Utilities:**
- `src/monkeybot/core/context.py` — `_scan_skills()` for the config-scan → prompt text pattern (reference for `to_prompt_block()` formatting)
- `src/monkeybot/core/scheduler.py` — `validate()` startup pattern

**Integration Points:**
- Story 1: `SubagentDefinition` defined there; imported here
- Phase 6: `SubagentRegistry` passed as optional arg to `AgentLoop.__init__`; `to_prompt_block()` appended to system prompt; `resolve(name)` called from `_dispatch_tool`

---

## Task Breakdown

### Task 1: `core/subagent_registry.py`

**Dependencies:** Story 1's `SubagentDefinition` (or stub)  
**Files**: `src/monkeybot/core/subagent_registry.py` (create)

**Signatures:**

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from monkeybot.core.subagent_proto import SubagentDefinition

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SubagentRegistry:
    def __init__(
        self,
        registry_block: dict[str, Any],
        *,
        bot_skills_path: str,
        bot_model: str,
        global_timeout: int = 300,
    ) -> None:
        """Validate and load all SubagentDefinitions.
        Raises ValueError if any name is invalid."""

    def resolve(self, name: str) -> SubagentDefinition:
        """Return definition for name.
        Raises KeyError: "No subagent '{name}'. Available: {names}" if not found."""

    def all_definitions(self) -> list[SubagentDefinition]:
        """Return all definitions in insertion order. Returns a copy."""

    def to_prompt_block(self) -> str:
        """Return markdown table for system prompt injection.
        Returns "" if registry is empty."""

    def validate(self) -> list[str]:
        """Check all script paths exist relative to Path.cwd().
        Returns list of error strings; empty = all OK."""
```

**Implementation Algorithm:**

`__init__`:
1. Iterate `registry_block.items()` in insertion order
2. For each `(name, cfg)`: validate name with `_NAME_RE.match(name)` — raise `ValueError(f"Invalid subagent name: '{name}'. Must match ^[a-z0-9][a-z0-9-]*$")` on mismatch
3. Require `cfg["description"]` — raise `ValueError` if missing/blank
4. Require `cfg["script"]` — raise `ValueError` if missing
5. Build `SubagentDefinition(name=name, script=cfg["script"], description=cfg["description"], skills_path=cfg.get("skills_path", bot_skills_path), model=cfg.get("model", bot_model), timeout_seconds=int(cfg.get("timeout_seconds", global_timeout)))`
6. Store in `self._definitions: dict[str, SubagentDefinition]` (ordered dict, insertion order preserved)

`resolve`: return `self._definitions[name]` — let `KeyError` propagate with a formatted message

`all_definitions`: return `list(self._definitions.values())`

`to_prompt_block`:
- If empty, return `""`
- Return:
  ```
  ## Available Subagents
  | Name | Description |
  |------|-------------|
  | researcher | Searches the web... |
  ```
  Build with f-string or `"\n".join(...)` — no third-party table lib

`validate`:
- For each definition, check `Path(definition.script).exists()` (relative to `Path.cwd()`)
- Collect error strings: `f"subagent '{definition.name}': script '{definition.script}' not found"`
- Return list (empty = OK)

---

### Task 2: Unit Tests

**Dependencies:** Task 1  
**Files**: `tests/unit/test_subagent_registry.py` (create)

**Pattern:** Follow `tests/unit/test_history.py` — pure sync tests are fine here (no async needed).

**Test Cases:**

```python
from monkeybot.core.subagent_registry import SubagentRegistry

REGISTRY_BLOCK = {
    "researcher": {
        "script": "subagents/researcher.py",
        "description": "Searches the web and summarizes findings.",
        "model": "gemini-2.0-pro",
        "skills_path": ".agents/skills/research",
        "timeout_seconds": 120,
    },
    "reviewer": {
        "script": "subagents/reviewer.py",
        "description": "Reviews a draft for clarity.",
    },
}

def make_registry(**overrides):
    return SubagentRegistry(
        REGISTRY_BLOCK,
        bot_skills_path=".agents/skills",
        bot_model="gemini-2.0-flash",
        global_timeout=300,
        **overrides,
    )
```

Remaining tests (concise form):
- `test_resolve_known_name`: `make_registry().resolve("researcher")` → `SubagentDefinition` with `name="researcher"`, `model="gemini-2.0-pro"`, `timeout_seconds=120`
- `test_resolve_unknown_name`: `resolve("unknown")` raises `KeyError`; error message contains `"unknown"` and `"researcher"`, `"reviewer"`
- `test_fallback_to_bot_defaults`: `resolve("reviewer")` with no `model`/`skills_path` in yaml → `definition.model == "gemini-2.0-flash"`, `definition.skills_path == ".agents/skills"`
- `test_to_prompt_block_contains_names`: `to_prompt_block()` contains `"researcher"` and `"reviewer"` and `"## Available Subagents"`
- `test_to_prompt_block_empty`: `SubagentRegistry({}, bot_skills_path="x", bot_model="y").to_prompt_block() == ""`
- `test_validate_missing_script`: write a registry with a script that doesn't exist; `validate()` returns a list with one string containing the subagent name and script path
- `test_validate_all_present(tmp_path)`: create the script file in `tmp_path`, chdir to `tmp_path`, `validate()` returns `[]`
- `test_invalid_name_raises_valueerror`: `SubagentRegistry({"BadName": {...}}, ...)` raises `ValueError`
- `test_all_definitions_returns_copy`: mutating `all_definitions()` result doesn't affect registry internals

---

### Task 3: Update `bots/example-bot/config.yaml`

**Dependencies:** Task 1  
**Files**: `bots/example-bot/config.yaml` (modify — add `subagents:` block only)

**Change:** Append a commented `subagents:` block after the existing content. Do not touch any existing keys.

```yaml
# --- Subagent registry (E4) ---
# subagents:
#   timeout_seconds: 300
#   registry:
#     researcher:
#       script: "subagents/researcher.py"
#       description: "Searches the web and summarizes findings on a given topic."
#       skills_path: ".agents/skills/research"  # optional
#       model: "gemini-2.0-pro"                 # optional
#       timeout_seconds: 120                    # optional
```

**Note:** Keep commented out — the example bot doesn't ship with scripts, so an active registry would fail `validate()`.

---

## Final Verification

**Functionality:**
- [ ] `resolve(known)` returns fully-populated `SubagentDefinition` with fallback values applied
- [ ] `resolve(unknown)` raises `KeyError` with helpful message
- [ ] `to_prompt_block()` contains all names and descriptions in markdown table format
- [ ] `to_prompt_block()` returns `""` for empty registry
- [ ] `validate()` returns error strings for missing scripts; `[]` when all exist
- [ ] Invalid name at `__init__` raises `ValueError` immediately

**Code Quality:**
- [ ] `ruff check src/monkeybot/core/subagent_registry.py` passes
- [ ] `mypy --strict src/monkeybot/core/subagent_registry.py` passes

**Testing:**
- [ ] All 9 test cases pass
