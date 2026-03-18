# Skills System

Skills are the mechanism for adding reusable, discoverable capabilities to your monkey-bot agent. They live as files on disk — not imported in code — which means you can add, update, or remove skills without touching your agent's main Python code.

---

## How Skills Work

```
./skills/
└── my-skill/
    ├── SKILL.md          ← Metadata: name, description, version
    └── my_skill.py       ← The actual tool implementation

On startup:
  SkillLoader scans ./skills/
       ↓
  Parses SKILL.md frontmatter from each subdirectory
       ↓
  Generates a skills manifest
       ↓
  Injects manifest into Layer 1 of the system prompt
       ↓
  Agent knows: "I have a skill called X, its entry point is at path Y"
       ↓
  Agent reads the file and invokes it when needed
```

The key difference from regular tools: the agent reads the skill's source code before calling it, and invokes it via `execute`. This allows the agent to understand the tool deeply and adapt its usage based on context.

---

## Creating a Skill

### Step 1: Create the directory

```bash
mkdir skills/my-skill
```

### Step 2: Write SKILL.md

```markdown
---
name: my-skill
description: A brief description of what this skill does and when to use it
version: 1.0.0
author: your-name
entry_point: skills/my-skill/my_skill.py
---

# My Skill

Detailed description of the skill's capabilities.

## Functions

- `do_thing(param: str) -> str` — Does a specific thing
- `do_other_thing(x: int, y: int) -> int` — Does another thing

## Usage

The agent should use this skill when the user asks about X.
Always pass the full path when calling read operations.
```

**Required frontmatter fields:**
- `name` — Skill identifier (lowercase, hyphens)
- `description` — One sentence, shown in the skills manifest
- `entry_point` — Relative path to the Python file

**Optional frontmatter fields:**
- `version` — Semantic version
- `author` — Skill author

### Step 3: Write the Python entry point

```python
# skills/my-skill/my_skill.py
from langchain_core.tools import tool

@tool
def do_thing(param: str) -> str:
    """Do a specific thing with the given parameter.
    
    Args:
        param: Description of what this parameter is for
        
    Returns:
        The result of doing the thing
    """
    # Your implementation
    return f"Did the thing with: {param}"

@tool  
def do_other_thing(x: int, y: int) -> int:
    """Add two numbers together.
    
    Args:
        x: First number
        y: Second number
        
    Returns:
        Sum of x and y
    """
    return x + y
```

---

## Built-in Skills

monkey-bot ships with three built-in skills:

### file-ops

Read, write, list, and manage files in the agent's workspace.

```
skills/file-ops/
├── SKILL.md
└── file_ops.py
```

Tools:
- `read_file(path: str)` — Read file contents
- `write_file(path: str, content: str)` — Write to file
- `list_files(directory: str)` — List directory contents
- `delete_file(path: str)` — Delete a file

### memory

Read and write to the agent's memory directory.

```
skills/memory/
├── SKILL.md
└── memory.py
```

Tools:
- `read_memory(key: str)` — Read a memory file by name
- `write_memory(key: str, content: str)` — Write to memory
- `list_memory()` — List all memory files

### search-web

Search the web for information.

```
skills/search-web/
├── SKILL.md
└── search_web.py
```

Tools:
- `search_web(query: str, num_results: int = 5)` — Search and return results

---

## Reference Skill: diagnostics

The `test-monkey` reference bot includes a `diagnostics` skill that's a good template for new skills:

```python
# skills/diagnostics.py
import os
import sys
import platform
from langchain_core.tools import tool

@tool
def run_diagnostics() -> str:
    """Run system diagnostics and return a health report.
    
    Checks environment variables, Python version, OS info,
    and basic computation to verify the agent is operating correctly.
    
    Returns:
        JSON string with diagnostic results and overall status (healthy/degraded)
    """
    results = {}
    
    # Check environment variables
    required_vars = ["AGENT_NAME", "MODEL_PROVIDER", "VERTEX_AI_PROJECT_ID"]
    env_status = {}
    for var in required_vars:
        env_status[var] = "set" if os.getenv(var) else "missing"
    results["environment"] = env_status
    
    # System info
    results["system"] = {
        "python_version": sys.version,
        "os": platform.system(),
        "platform": platform.platform(),
        "cwd": os.getcwd(),
    }
    
    # Computation check
    expected = sum(range(1000))
    actual = sum(i for i in range(1000))
    results["computation"] = {
        "expected": expected,
        "actual": actual,
        "passed": expected == actual,
    }
    
    # Overall status
    all_env_set = all(v == "set" for v in env_status.values())
    computation_ok = results["computation"]["passed"]
    results["status"] = "healthy" if all_env_set and computation_ok else "degraded"
    
    import json
    return json.dumps(results, indent=2)
```

---

## Advanced: Async Skills

Skills can be async for I/O-heavy operations:

```python
# skills/api-client/api_client.py
import httpx
from langchain_core.tools import tool

@tool
async def fetch_data(endpoint: str, query: str) -> str:
    """Fetch data from the internal API.
    
    Args:
        endpoint: API endpoint path (e.g., /users, /orders)
        query: Search query string
        
    Returns:
        JSON response from the API
    """
    base_url = os.getenv("API_BASE_URL", "https://api.internal.example.com")
    api_key = os.getenv("API_KEY")
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{base_url}{endpoint}",
            params={"q": query},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.text
```

---

## Skills Directory Structure

You can organize skills in any way. The loader scans all subdirectories for `SKILL.md` files:

```
skills/
├── diagnostics.py         ← Single-file skill (also valid)
├── customer-support/
│   ├── SKILL.md
│   └── customer_support.py
├── data-analysis/
│   ├── SKILL.md
│   ├── analysis.py
│   └── helpers.py         ← Helper modules (not exposed as tools)
└── integrations/
    ├── slack/
    │   ├── SKILL.md
    │   └── slack_skill.py
    └── jira/
        ├── SKILL.md
        └── jira_skill.py
```

---

## Skills vs Tools

| | Tools | Skills |
|---|---|---|
| **Definition** | `@tool` functions in `src/main.py` | `@tool` functions in `skills/` directory |
| **Discovery** | Passed explicitly to `build_deep_agent()` | Auto-discovered from filesystem |
| **Agent access** | Available immediately | Read from file then executed |
| **Best for** | Core, always-available capabilities | Reusable, optional capabilities |
| **Example** | `schedule_task`, `search_memory` | `run_diagnostics`, `fetch_customer_data` |

**When to use tools:**
- Always-on capabilities the agent needs for every task
- Framework-provided tools (scheduler, memory search)
- Simple utilities tightly coupled to your agent

**When to use skills:**
- Reusable capabilities shared across multiple bots
- Complex tools with substantial business logic
- Tools you might add/remove without changing agent code

---

## Testing Skills

```python
# tests/test_diagnostics.py
import pytest
from skills.diagnostics import run_diagnostics

def test_run_diagnostics_returns_health_status():
    result = run_diagnostics.invoke({})
    import json
    data = json.loads(result)
    assert "status" in data
    assert data["status"] in ("healthy", "degraded")
    assert "system" in data
    assert "computation" in data

def test_computation_check_passes():
    result = run_diagnostics.invoke({})
    import json
    data = json.loads(result)
    assert data["computation"]["passed"] is True
```

Run:

```bash
python -m pytest tests/ -v
```

---

## Skills Manifest

When the agent starts, it receives a skills manifest in Layer 1 of the system prompt. This manifest lists every available skill with its description and entry point path.

Example manifest (generated automatically):

```
## Available Skills

### diagnostics
Description: Run system diagnostics and return a health report
Entry point: /app/skills/diagnostics.py
Version: 1.0.0

### file-ops
Description: Read, write, and list files in the workspace
Entry point: /app/skills/file-ops/file_ops.py
Version: 1.0.0

Usage: To use a skill, first read its entry point file, then call the appropriate function.
The exact file paths above are absolute and correct for this deployment.
```

The agent reads the source file to understand exactly what parameters to pass before calling the tool.
