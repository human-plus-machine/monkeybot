# Monkey-Bot Examples

This directory contains example implementations and patterns for building agents with the monkey-bot framework.

## Examples

### Skills

Example skills demonstrating the skill pattern:

#### [Diagnostics Skill](skills/diagnostics/)

A complete reference implementation showing:
- Using `@tool` decorator for LangChain integration
- Async execution patterns
- Structured output (JSON)
- Environment variable checks
- Error handling
- Logging best practices

**Use this as a template** when creating your own skills.

## How to Use Examples

### Copy to Your Deployment

```bash
# Copy an example skill to your deployment
cp -r examples/skills/diagnostics/ ./skills/

# The skill is now available to your agent
```

### Import in Your Code

```python
from monkeybot import build_deep_agent
from skills.diagnostics import run_diagnostics

# Use directly
result = await run_diagnostics.ainvoke({"check_type": "full"})

# Or load via SkillLoader
from monkeybot.skills import SkillLoader, SkillExecutor

loader = SkillLoader(skills_dir="./skills")
executor = SkillExecutor(loader.load_skills())
tools = executor.to_langchain_tools()

agent = build_deep_agent(model="gemini-2.5-flash", tools=tools)
```

## Creating Your Own Examples

Have a great example to share? We'd love to see it!

1. Create your example in the appropriate subdirectory
2. Include a detailed README with:
   - What the example demonstrates
   - How to use it
   - How to adapt it
   - Key patterns highlighted
3. Test that it works from a fresh clone
4. Submit a PR to the repository

## Additional Resources

- [Getting Started (v2 gateway)](../docs/getting-started.md)
- [Configuration reference](../.env.example)
- [Architecture overview](../README.md)
- HTTP API: `monkeybot.gateway.sse.routes` (`POST /sessions`, `GET /sessions/{id}/events`, `POST /sessions/{id}/reply`, `GET /health`)
