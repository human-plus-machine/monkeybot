# Code Spec: Story 4 — Infrastructure & Bot Template

**Story:** user_stories.md "Story 4: Infrastructure & Bot Template"  
**Design Reference:** 1b-contracts.md "Public Module Surface", 1c-operations.md "scripts/bootstrap Update", monkeybot_v2_plan.md "AGENT.md Template"  
**Date:** 2026-05-13  
**Complexity:** XS

## Implementation Summary
- **Files to Create:** 4 (bots/example-bot/ + .env.example)
- **Files to Modify:** 4 (scripts/bootstrap, scripts/run, scripts/test, src/monkeybot/__init__.py)
- **Estimated LOC:** ~120 total

---

## Task 1: `src/monkeybot/__init__.py`

**Files:** `src/monkeybot/__init__.py` (modify — currently empty)

**Critical:** Use `__getattr__` for lazy imports. Do NOT add top-level `from ... import ...` statements. The entire file must execute in < 5ms.

```python
"""MonkeyBot v2 — lightweight agent framework."""
from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["AgentLoop", "ConversationHistory", "Provider", "TurnContext"]


def __getattr__(name: str) -> object:
    if name == "AgentLoop":
        from monkeybot.core.loop import AgentLoop
        return AgentLoop
    if name == "ConversationHistory":
        from monkeybot.core.history import ConversationHistory
        return ConversationHistory
    if name == "Provider":
        from monkeybot.core.provider import Provider
        return Provider
    if name == "TurnContext":
        from monkeybot.core.context import TurnContext
        return TurnContext
    raise AttributeError(f"module 'monkeybot' has no attribute {name!r}")
```

**Verification:** `python -c "import monkeybot; print(monkeybot.__version__)"` must complete in < 200ms.

---

## Task 2: `scripts/bootstrap`

**Files:** `scripts/bootstrap` (modify existing)

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "Installing dependencies..."
uv sync --extra gemini --extra dev

echo "Setting up environment..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "→ Created .env from .env.example"
    echo "→ Edit .env and set GEMINI_API_KEY before running"
else
    echo "→ .env already exists, skipping"
fi

echo "Creating data directories..."
mkdir -p data/memory

echo ""
echo "Bootstrap complete."
echo "Next steps:"
echo "  1. Edit .env and set GEMINI_API_KEY"
echo "  2. Run: scripts/run"
```

---

## Task 3: `scripts/run` and `scripts/test`

**`scripts/run`:**
```bash
#!/usr/bin/env bash
set -euo pipefail
# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
fi
exec python -m monkeybot run --bot-dir "${BOT_DIR:-./bots/example-bot}"
```

**`scripts/test`:**
```bash
#!/usr/bin/env bash
set -euo pipefail
exec python -m pytest "$@"
```

All 3 scripts must be `chmod +x`. Verify with `ls -la scripts/`.

---

## Task 4: `.env.example`

**Files:** `.env.example` (create)

```bash
# Required: LLM Provider
MODEL_PROVIDER=gemini
GEMINI_API_KEY=your-key-here

# Storage: defaults work for local dev
DB_URL=sqlite:///data/monkeybot.db
MEMORY_PATH=./data/memory
SKILLS_PATH=./.agents/skills

# Bot identity
AGENT_MD_PATH=./bots/example-bot/AGENT.md
BOT_DIR=./bots/example-bot

# Logging
LOG_LEVEL=INFO
```

---

## Task 5: `bots/example-bot/`

**Files:** `bots/example-bot/AGENT.md`, `bots/example-bot/config.yaml`, `bots/example-bot/MEMORY.md`

**`AGENT.md`** — follow template from monkeybot_v2_plan.md "AGENT.md Template":

```markdown
# ExampleBot

## Identity
You are ExampleBot, a helpful general-purpose assistant.
You are concise, accurate, and transparent about what you can and cannot do.

## Capabilities
- You have access to five tools: run_command, read_file, write_file, search_memory, list_skills
- Use list_skills() to discover available capabilities
- Use search_memory() before answering questions that might be covered by past context
- Use write_file() to save important information to your memory directory

## Behavior
- Before taking any significant action, state what you're about to do
- Prefer reading existing files over assuming their contents
- When using run_command, prefer specific scripts over raw shell commands
- Always check search_memory before claiming you don't know something

## Memory
- Save important facts to the memory directory via write_file
- Use the path format: {MEMORY_PATH}/topic-name.md
- Search memory before answering questions about past context

## Limitations
- Cannot access the internet directly (use run_command with curl via a skill)
- Cannot modify files outside the bot directory and memory directory
- Commands must complete within 30 seconds (configurable via timeout)
```

**`config.yaml`** — safety configuration for E1:

```yaml
safety:
  denied_patterns:
    - "sudo"
    - "rm -rf /"
    - "curl.*|.*bash"
    - "wget.*|.*bash"
    - "mkfs"
    - ":(){:|:&};:"
  pre_approved:
    - "echo"
    - "ls"
    - "cat"
    - "python"
    - "uv"
    - "pwd"
    - "date"

model:
  default: "gemini-2.0-flash"
  max_tokens: 8192
```

**`MEMORY.md`** — initial memory file:

```markdown
# ExampleBot Memory

This bot was initialized on first run.
Use write_file to add memory files to the memory directory.
Use search_memory to find information from past sessions.
```

---

## Final Verification

- [ ] `python -c "import monkeybot; print(monkeybot.__version__)"` prints `"2.0.0"` and completes < 200ms
- [ ] `scripts/bootstrap` runs from clean clone without error
- [ ] All 3 scripts are executable (`chmod +x`)
- [ ] `.env.example` has all 7 vars with comments
- [ ] `bots/example-bot/AGENT.md` follows the template
- [ ] `bots/example-bot/config.yaml` has `safety.denied_patterns` and `safety.pre_approved`
- [ ] `ruff check src/monkeybot/__init__.py` passes
- [ ] `mypy --strict src/monkeybot/__init__.py` passes
