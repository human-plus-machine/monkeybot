# Monkey-Bot Examples

This directory contains example implementations and patterns for building agents with the monkey-bot framework.

## Examples

### Skills

Example skills demonstrating the skill pattern:

#### [Diagnostics Skill](skills/diagnostics/)

A complete reference implementation showing:
- ``SKILL.md`` for harness discovery (required for ``list_skills``)
- ``python3`` via ``run_command`` with workspace-relative ``argv`` (see ``SKILL.md``)
- Optional ``@tool`` decorator for direct Python imports
- Async execution patterns
- Structured output (JSON)
- Environment variable checks
- Error handling
- Logging best practices

**Use this as a template** when creating your own skills.

## How to Use Examples

### Copy to Your Deployment

```bash
# Copy an example skill into the default SKILLS_PATH
cp -r examples/skills/diagnostics/ ./.agents/skills/

# The skill is now available to your agent
```

### Import in Your Code

```python
from skills.diagnostics.diagnostics import run_diagnostics

# Invoke the async skill entry point directly (or load via SkillLoader per docs)
result = await run_diagnostics(check_type="full")
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
- [Harness template](../monkeybot_config/monkeybot.example.yaml)
- [Architecture overview](../README.md)
- HTTP API: `monkeybot.gateway.sse.routes` (`POST /sessions`, `GET /sessions/{id}/events`, `POST /sessions/{id}/reply`, `GET /health`)
