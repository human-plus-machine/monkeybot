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
export OLLAMA_API_KEY=…          # ollama.com/settings/keys; extra is monkeybot[ollama]
# model.provider / model.name come from monkeybot_config/monkeybot.yaml (YAML-only)
export PORT=8787
export SANDBOX_ENABLED=false
uv run -m monkeybot.gateway.main

# Terminal 2 — scorecard (repo root)
uv sync --extra evals
export AGENT_URL=http://127.0.0.1:8787
export JUDGE_PROVIDER=fake       # deepeval's GPTModel against ollama.com times out with no scores
export EVAL_SCENARIO_DELAY_SEC=10   # space scenarios out if the model gets rate-limited
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
| `--scenario-delay-sec` (or `EVAL_SCENARIO_DELAY_SEC`) | Sleep this many seconds between scenarios — a scenario itself fires several requests in a tight burst, so pacing is applied between scenarios, not within one. Use when a low-rpm model gets provider-side rate-limited. |

Runs land under `evals/runs/` (gitignored). Diff two artifacts with `uv run python -m evals.diff <old.json> <new.json>`.

## Drift suite (paired verifier off / on)

The drift suite (`evals/suites/drift.yaml`, scenarios under `evals/scenarios/drift/`) is **not** in smoke. It exists to measure verifier precision/recall once Phase 2+ lands. Cases `supersession` and `preempt` assert `max_verdicts: 0` with the verifier on — they are the precision half.

The gateway discovers `monkeybot_config/monkeybot.yaml` from cwd, so a second filename is never loaded unless you point `MONKEYBOT_CONFIG` at it (bootstrap pointer, not a YAML value).

```bash
# Terminal 1a — verifier off (default smoke config)
cd evals/smoke_agent
uv sync
export OLLAMA_API_KEY=…
export PORT=8787
export SANDBOX_ENABLED=false
uv run -m monkeybot.gateway.main

# Terminal 2a
cd ../..   # repo root
uv sync --extra evals
export AGENT_URL=http://127.0.0.1:8787
export EVAL_VERIFIER_MODE=off
uv run python -m evals.report --suite drift
# note the run id printed / the new file under evals/runs/

# Stop the gateway, then boot with the verifier-on file:
cd evals/smoke_agent
export MONKEYBOT_CONFIG=./monkeybot_config/monkeybot.verifier-on.yaml
uv run -m monkeybot.gateway.main

# Terminal 2b
export EVAL_VERIFIER_MODE=on
uv run python -m evals.report --suite drift
uv run python -m evals.diff evals/runs/<off-run>.json evals/runs/<on-run>.json
```

Until a consumer reads `verifier:` (Phase 1+), the on-config is a parsed no-op and the on/off pair should be identical besides the YAML digest.

## Smoke baseline (pre-verifier)

Committed at [`evals/baselines/smoke.json`](../evals/baselines/smoke.json); readable summary in [`evals/baselines/smoke.md`](../evals/baselines/smoke.md). Agent is `ollama-cloud` / `glm-5.3-flash`. Judge scores are empty (`JUDGE_PROVIDER=fake`) — deepeval's OpenAI-compatible judge against Ollama Cloud timed out with no metric scores.

```bash
cd evals/smoke_agent && uv sync
OLLAMA_API_KEY=… PORT=8787 SANDBOX_ENABLED=false uv run -m monkeybot.gateway.main &
cd ../.. && AGENT_URL=http://127.0.0.1:8787 JUDGE_PROVIDER=fake \
  uv run python -m evals.report --suite smoke --update-baseline
```

Do not add `--require-baseline` to CI until that workflow uses the same provider as this file. The GitHub Action still keys off `NVIDIA_API_KEY`.

## Coding suite (workspace-checked)

`evals/suites/coding.yaml` (scenarios under `evals/scenarios/coding/`) measures whether the agent can navigate, edit and verify real code. It is **not** in smoke: it needs `pytest` in the agent's environment (listed in `evals/smoke_agent/pyproject.toml`) and a model that can edit code.

A scenario names a `workspace_fixture` (a directory under `evals/`, here `fixtures/coding/calc_repo/`). Before each run the report CLI copies it to `<agent workspace>/eval-<scenario id>/` (fresh every run) and substitutes `{workspace_subdir}` into the messages. The agent workspace is `MONKEYBOT_AGENT_WORKSPACE`, default `evals/smoke_agent/workspace` — set it to match the gateway's `paths.workspace_root`.

Beyond telemetry, coding scenarios assert on the resulting files:

| Assertion | Passes when |
|---|---|
| `workspace_command_passes` | the command (argv-split, `python` = the eval interpreter) exits 0 in the copy |
| `workspace_file_contains` | `{path: substring}` — the file contains the substring |
| `workspace_unchanged` | listed paths are byte-identical to the fixture |
| `final_text_regex` | the last turn's text matches |
| `verified_before_final` | the last verify command (`true` = pytest/npm test/cargo test/…, or a regex) succeeded and no write tool ran after it |

```bash
# gateway: cd evals/smoke_agent && uv sync && uv run -m monkeybot.gateway.main   (as above)
export MONKEYBOT_AGENT_WORKSPACE=$PWD/evals/smoke_agent/workspace
uv run python -m evals.report --suite coding                      # take a baseline first
uv run python -m evals.report --suite coding --update-baseline    # -> evals/baselines/coding.json
uv run python -m evals.report --suite coding --fail-on-regression # after behavior changes
```

The deterministic counterpart lives in `tests/evals/`: scenarios with `executor: real` + `workspace_fixture:` run the scripted loop against a real `CoreToolExecutor` over a temp copy of the same fixture and may use `workspace_file_contains` / `workspace_command_passes`.

### Harder suite and repeated trials

`evals/suites/coding_hard.yaml` runs seven tasks over `fixtures/coding/shop_repo` (about 25 files with decoy copies of the code under `shop/legacy/`, `vendor/` and `build/`): a bug whose cause is one module away, a feature that must follow `AGENTS.md` conventions (type hints, docstring, test, CHANGELOG entry), a test run that prints ~230 KB before the failure, a navigation question, a rename, an unfixable failure that needs an honest report, and a four-part change. Seeded bugs are `workspace_overlays` copied over the fixture. `evals/test_coding_hard_oracle.py` proves each scenario is solvable and fails when nothing is done, so a live failure is the agent's.

One LLM run proves little. `evals/trials.py` runs a suite N times against a gateway and reports per-scenario pass counts:

```bash
uv run python -m evals.trials --suite coding_hard --trials 6 --agent-url http://127.0.0.1:8787 \
  --label branch-coding --out /tmp/branch-coding.json
uv run python -m evals.trials --compare /tmp/a.json /tmp/b.json /tmp/c.json
```

To compare harness versions, boot one gateway per arm in its own copy of `evals/smoke_agent/` (separate `workspace/`, `memory/`, `data/`) and point `PYTHONPATH` at the other checkout's `src/`. The runner answers any `ask_user` prompt with "no one is available" so a model that stops to ask cannot hang a run.

## What the smoke suite covers

Scenarios under `evals/scenarios/` assert both judge metrics and harness telemetry (e.g. `required_tools`). Current smoke membership is the explicit list in `evals/suites/smoke.yaml` (tools, skills, subagents, memory, multi-turn, mcp — `context/summarization_trigger` is currently commented out, see `evals/TEST_COVERAGE.md`). MCP scenarios run against an in-process fixture MCP server (`evals/smoke_agent/fixture_mcp_server.py`, no network calls) wired up in `evals/smoke_agent/monkeybot_config/mcp.json` — see `evals/scenarios/mcp/`.

## CI

Live eval does **not** run on every feature PR. Triggers (see the workflow):

- PR from `develop` → `main` (release gate; uses `--fail-on-regression`)
- PR that changes `uv.lock`, `cli/uv.lock`, or `evals/smoke_agent/uv.lock`
- Push to `main` (report-only)
- Manual `workflow_dispatch`

CI still requires repository secret `NVIDIA_API_KEY` (legacy agent + judge on build.nvidia.com). Local smoke uses `OLLAMA_API_KEY` and `monkeybot.yaml`'s `ollama-cloud` / `glm-5.3-flash`. The scorecard is posted to the run summary and as a PR comment.

## vs `tests/evals/`

| | `tests/evals/` | `evals/` (this doc) |
|---|---|---|
| Driver | `loop.run` + fake provider | Live gateway HTTP + SSE |
| CI | Every push/PR via pytest | Selective (above) |
| Catches | Deterministic loop/tool wiring | Real provider + gateway/SSE behavior |
