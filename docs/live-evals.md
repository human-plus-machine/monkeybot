# Live evals

T4 runs a real provider against a live gateway and writes a scorecard. Tiers, gates, and the failure policy are in [testing.md](testing.md).

**Agent project:** [`regression/t4/agent/`](../regression/t4/agent/)
**Suites:** [`regression/t4/suites/`](../regression/t4/suites/)
**Workflow:** [`.github/workflows/live-eval-smoke.yml`](../.github/workflows/live-eval-smoke.yml) is manual only. The smoke suite runs on this machine. GitHub does not receive `OLLAMA_API_KEY`.

## Local smoke

`OLLAMA_API_KEY` comes from the environment or the repo-root `.env`. The judge is `ollama-cloud` / `glm-5.3-flash` in [`regression/t4/eval_config.yaml`](../regression/t4/eval_config.yaml). `JUDGE_PROVIDER` and `JUDGE_MODEL` still override that file.

```bash
# Terminal 1 — gateway
cd regression/t4/agent
uv sync
export PORT=8787
export SANDBOX_ENABLED=false
uv run -m monkeybot.gateway.main

# Terminal 2 — scorecard (repo root)
uv sync --extra evals
export AGENT_URL=http://127.0.0.1:8787
uv run python -m regression.t4 \
  --suite smoke \
  --baseline regression/t4/baselines/smoke.json
```

| Flag | Effect |
|---|---|
| `--fail-on-regression` | Exit non-zero on scenario errors or failures, and on baseline drift when a baseline is present |
| `--require-baseline` | Fail if the baseline file is missing |
| `--update-baseline` | Rewrite the baseline from this run |
| `--scenario-delay-sec` (or `EVAL_SCENARIO_DELAY_SEC`) | Sleep between scenarios |

Runs land under `regression/t4/runs/` (gitignored). Diff two artifacts with `uv run python -m regression.t4.diff <old.json> <new.json>`.

The committed smoke baseline is [`regression/t4/baselines/smoke.json`](../regression/t4/baselines/smoke.json), with the readable summary in [`smoke.md`](../regression/t4/baselines/smoke.md).

## Drift suite

[`regression/t4/suites/drift.yaml`](../regression/t4/suites/drift.yaml) stays out of smoke. Run it twice. `EVAL_VERIFIER_MODE=off` uses the default gateway config. `EVAL_VERIFIER_MODE=on` makes the runner start [`monkeybot.verifier-tracker.yaml`](../regression/t4/agent/monkeybot_config/monkeybot.verifier-tracker.yaml), unless `EVAL_VERIFIER_CONFIG` names another file. The judge config is [`monkeybot.verifier-judge.yaml`](../regression/t4/agent/monkeybot_config/monkeybot.verifier-judge.yaml), selected at boot with `MONKEYBOT_CONFIG`.

```bash
cd regression/t4/agent
export PORT=8787 SANDBOX_ENABLED=false
uv run -m monkeybot.gateway.main

# repo root, verifier off
export AGENT_URL=http://127.0.0.1:8787 EVAL_VERIFIER_MODE=off
uv run python -m regression.t4 --suite drift

# stop the gateway, then boot the on-config and rerun
cd regression/t4/agent
export MONKEYBOT_CONFIG=./monkeybot_config/monkeybot.verifier-tracker.yaml
uv run -m monkeybot.gateway.main
export EVAL_VERIFIER_MODE=on
uv run python -m regression.t4 --suite drift
```

## Coding suite

[`regression/t4/suites/coding.yaml`](../regression/t4/suites/coding.yaml) and [`coding_hard.yaml`](../regression/t4/suites/coding_hard.yaml) are separate from smoke. A scenario's `workspace_fixture` is a directory under [`regression/fixtures/workspaces/`](../regression/fixtures/workspaces/). The runner copies it into the agent workspace (default [`regression/t4/agent/workspace`](../regression/t4/agent/workspace), or `MONKEYBOT_AGENT_WORKSPACE`) and substitutes `{workspace_subdir}` into the messages.

[`regression/selftest/test_coding_hard_oracle.py`](../regression/selftest/test_coding_hard_oracle.py) proves each hard scenario is solvable and fails when nothing is done. Repeated trials of a T4 suite are `uv run python -m regression.t5 --suite <suite> --trials N`. Keyed baselines for that compare are written by `--update-baseline` on `regression.t5`.

Scripted coverage of the same harness lives in `regression/t2`, collected by `uv run pytest regression/t2`.

## CI

Live eval does not run on push. `.github/workflows/live-eval-smoke.yml` is `workflow_dispatch` only, and it has no provider key, so a GitHub run stops in preflight. The local command is:

```bash
uv run python -m regression.t4 --suite smoke
```
