---
name: diagnostics
description: Run deployment health checks (env, runtime, computation) and return JSON status.
---

# diagnostics

Verify the agent harness, shell allowlist, and Python runtime are wired correctly.

## When to use

- After first deploy or cold start
- When the user asks whether the agent environment is healthy

## How to run

Shell commands start in **workspace root**. Trusted skill files live in the
separate, read-only `SKILLS_PATH`; use that environment variable when invoking
a bundled script.

1. `list_skills` should include `diagnostics`.
2. Run the script with `run_command`:

```json
{"argv": ["bash", "-c", "python3 \"$SKILLS_PATH/diagnostics/diagnostics.py\""]}
```
3. Parse the JSON stdout; `status` should be `healthy` when required env vars are set.

## Expected env vars (full check)

`AGENT_NAME`, `VERTEX_AI_PROJECT_ID`, `SKILLS_DIR` — missing vars appear in `issues`.
