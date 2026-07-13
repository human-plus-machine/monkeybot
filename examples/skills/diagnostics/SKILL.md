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

Shell commands start in **workspace root** (see harness Runtime paths). Use **workspace-relative** paths only — not absolute container paths unless they match an allowed prefix.

1. `list_skills` should include `diagnostics`.
2. Run the script with `run_command` and `argv` (scaffolded `SKILLS_PATH` is `./workspace/skills`):

```json
{"argv": ["python3", "./skills/diagnostics/diagnostics.py"]}
```
3. Parse the JSON stdout; `status` should be `healthy` when required env vars are set.

## Expected env vars (full check)

`AGENT_NAME`, `MODEL_PROVIDER`, `VERTEX_AI_PROJECT_ID`, `SKILLS_DIR` — missing vars appear in `issues`.
