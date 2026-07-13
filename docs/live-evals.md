# Live evals

Run real-provider smoke scenarios against a live monkeybot gateway, score them, and optionally compare to a baseline. Complements deterministic loop evals under `tests/evals/` (fake provider / pytest).

**Fixture agent:** [`evals/smoke_agent/`](../evals/smoke_agent/)  
**Workflow:** [`.github/workflows/live-eval-smoke.yml`](../.github/workflows/live-eval-smoke.yml)  
**Suite:** [`evals/suites/smoke.yaml`](../evals/suites/smoke.yaml)

## Local run

```bash
# Terminal 1 — gateway
cd evals/smoke_agent
uv sync
export NVIDIA_API_KEY=…          # or another provider + matching monkeybot extra
export MODEL_PROVIDER=nvidia
export MODEL_NAME=meta/llama-3.3-70b-instruct
export PORT=8787
export SANDBOX_ENABLED=false
uv run -m monkeybot.gateway.main

# Terminal 2 — scorecard (repo root)
uv sync --extra evals
export AGENT_URL=http://127.0.0.1:8787
export JUDGE_PROVIDER=nvidia
export JUDGE_MODEL=nvidia/llama-3.3-nemotron-super-49b-v1
export NVIDIA_API_KEY=…
uv run python -m evals.report \
  --suite smoke \
  --baseline evals/baselines/smoke.json
```

Useful flags:

| Flag | Effect |
|---|---|
| `--fail-on-regression` | Exit non-zero on scenario errors/failures (and baseline drift when a baseline is present) |
| `--require-baseline` | Fail if the baseline file is missing |
| `--update-baseline` | Rewrite the baseline from this run (commit deliberately) |

Runs land under `evals/runs/` (gitignored). Diff two artifacts with `uv run python -m evals.diff <old.json> <new.json>`.

## What the smoke suite covers

Scenarios under `evals/scenarios/` assert both judge metrics and harness telemetry (e.g. `required_tools`). Current smoke membership is the explicit list in `evals/suites/smoke.yaml` (tools, skills, subagents, memory, context, multi-turn). MCP live scenarios are local-only until a CI MCP fixture exists — see `evals/scenarios/mcp/tool_invoke.yaml`.

## CI

Live eval does **not** run on every feature PR. Triggers (see the workflow):

- PR from `develop` → `main` (release gate; uses `--fail-on-regression`)
- PR that changes `uv.lock`, `cli/uv.lock`, or `evals/smoke_agent/uv.lock`
- Push to `main` (report-only)
- Manual `workflow_dispatch`

Requires repository secret `NVIDIA_API_KEY` (agent + judge on build.nvidia.com). The scorecard is posted to the run summary and as a PR comment.

## vs `tests/evals/`

| | `tests/evals/` | `evals/` (this doc) |
|---|---|---|
| Driver | `loop.run` + fake provider | Live gateway HTTP + SSE |
| CI | Every push/PR via pytest | Selective (above) |
| Catches | Deterministic loop/tool wiring | Real provider + gateway/SSE behavior |
