# Diagnostics Skill Example

This example demonstrates how to create an async skill entry point for monkey-bot.

## What It Does

The diagnostics skill runs system health checks including:
- **Environment Variables**: Checks if required env vars are set
- **Python Runtime**: Reports Python version and OS information
- **Computation Test**: Verifies basic computation works
- **Structured Output**: Returns results as JSON with clear pass/fail indicators

## How to Use

### 1. Copy to Your Deployment

```bash
# Copy the skill to your deployment's skills directory
cp -r examples/skills/diagnostics/ ./skills/
```

### 2. Import in Your Agent

```python
from skills.diagnostics.diagnostics import run_diagnostics

result = await run_diagnostics(check_type="full")
print(result)
```

Wire skills through the **SSE gateway** and harness tool surface described in `docs/getting-started.md` (this README focuses on the skill module itself).

Example JSON shape:

```json
{
  "timestamp": "2026-02-16T20:00:00Z",
  "status": "healthy",
  "issues": [],
  "python_version": "3.12.0",
  "os_info": "Linux-5.10.0",
  "cwd": "/app",
  "env_check": {
    "AGENT_NAME": "set",
    "MODEL_PROVIDER": "set",
    "VERTEX_AI_PROJECT_ID": "set",
    "SKILLS_DIR": "set"
  },
  "computation_check": "pass"
}
```

## How to Adapt

Want to create your own skill? Follow this pattern:

### 1. Copy the Skill File

```bash
cp skills/diagnostics/diagnostics.py skills/my-skill/my-skill.py
```

### 2. Modify the @tool Function

```python
from monkeybot.core.workspace_tools import tool

@tool
async def my_custom_tool(param1: str, param2: int = 10) -> str:
    """Tool description that the LLM reads to understand when to use it.
    
    Be specific about what the tool does and when to use it.
    
    Args:
        param1: First parameter description
        param2: Second parameter description (optional, default: 10)
    
    Returns:
        Description of what is returned
    """
    # Your implementation here
    result = f"Processed {param1} with {param2}"
    return result
```

### 3. Update Docstring

The LLM uses the docstring to understand:
- **What** the tool does
- **When** to use it
- **What** parameters it needs
- **What** it returns

Make your docstring clear and specific!

### 4. Test Your Skill

```python
from skills.my_skill.my_skill import my_custom_tool

# Test directly (async entry point)
result = await my_custom_tool(param1="test", param2=5)
print(result)
```

## Key Patterns Demonstrated

### 1. Using @tool Decorator

```python
from monkeybot.core.workspace_tools import tool

@tool
async def my_tool(param: str) -> str:
    """Tool description."""
    return f"Result: {param}"
```

The `@tool` decorator:
- Marks the function for tooling-style discovery in simple orchestrators
- Preserves the original name and docstring for documentation

### 2. Async Execution

```python
@tool
async def my_tool():  # ← Note: async
    result = await some_async_operation()
    return result
```

Skills should be **async** for non-blocking execution, especially if they:
- Make API calls
- Read/write files
- Perform I/O operations

### 3. Structured Output

```python
import json
from dataclasses import dataclass, asdict

@dataclass
class Result:
    status: str
    data: dict

@tool
async def my_tool() -> str:
    result = Result(status="success", data={"key": "value"})
    return json.dumps(asdict(result), indent=2)
```

Return JSON strings for structured data that the LLM can parse and present to users.

### 4. Error Handling

```python
@tool
async def my_tool(param: str) -> str:
    """Tool that handles errors gracefully."""
    try:
        result = risky_operation(param)
        return f"Success: {result}"
    except ValueError as e:
        return f"Error: Invalid input - {e}"
    except Exception as e:
        logger.error(f"Tool error: {e}", exc_info=True)
        return f"Error: {str(e)}"
```

Always handle errors gracefully:
- Return error messages as strings (don't raise exceptions)
- Log errors for debugging
- Give the LLM context about what went wrong

### 5. Logging

```python
import logging

logger = logging.getLogger(__name__)

@tool
async def my_tool():
    logger.info("Tool started")
    result = do_work()
    logger.info(f"Tool completed: {result}")
    return result
```

Log key events to help debug issues in production.

## Common Use Cases

**System Monitoring:**
```python
@tool
async def check_disk_space() -> str:
    """Check available disk space."""
    # Implementation
```

**External API Calls:**
```python
@tool
async def fetch_weather(city: str) -> str:
    """Get weather forecast for a city."""
    # Implementation with httpx
```

**Database Queries:**
```python
@tool
async def count_users() -> str:
    """Count total users in database."""
    # Implementation with async DB client
```

**File Operations:**
```python
@tool
async def read_config_file(filename: str) -> str:
    """Read configuration from file."""
    # Implementation with pathlib
```

## Testing Your Skill

Create a test file alongside your skill:

```python
# tests/test_my_skill.py
import pytest
from skills.my_skill.my_skill import my_custom_tool

@pytest.mark.asyncio
async def test_my_tool_success():
    """Test successful execution."""
    result = await my_custom_tool(param1="test")
    assert "test" in result

@pytest.mark.asyncio
async def test_my_tool_error_handling():
    """Test error handling."""
    result = await my_custom_tool(param1="")
    assert "Error" in result
```

## Best Practices

1. **Clear Docstrings**: The LLM reads these to decide when to call your tool
2. **Type Hints**: Use proper type annotations for all parameters
3. **Error Handling**: Return error messages as strings, don't raise exceptions
4. **Logging**: Log key events for debugging
5. **Structured Output**: Return JSON for complex data
6. **Async**: Use async for I/O operations
7. **Testing**: Write unit tests for your skills

## Next Steps

- Browse more examples in [`examples/skills/`](../)
- Read the [Skills Development Guide](../../docs/skills.md)
- Check out the [Architecture Overview](../../README.md)
