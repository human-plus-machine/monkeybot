# Phase 4 — Live tier (≈ 3–4 days)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

- Move `evals/` into `regression/t4/`, unified schema, selftests as pytest.
- Live setup per 5.11: Ollama Cloud agent and judge, `preflight.py`, `.env` fallback. Live smoke runs locally. GitHub does not get `OLLAMA_API_KEY` (D1).
- Add new T4 cases (`T4-FILE-04`, `T4-MCP-04`, `T4-SCHED-*`, `T4-CTX-01` retuned, `T4-CTX-02`, `T4-HOOK-01`, `T4-MEM-03`, `T4-SUB-02`, `T4-SKILL-01`, `T4-GW-02`).
- Split verifier configs.

Exit: smoke suite green on a local run against Ollama Cloud; scorecard from that run; run JSON convertible to cassettes; a failing live scenario produces a ledger record with `live` provider/model and the run JSON path in `artifacts`. GitHub does not run this suite (D1).

## Tasks

Order: P4-01 → P4-02 → P4-03 → P4-04 → P4-05 → P4-06. P4-01 and P4-03 need no network.

Every task that runs a live command needs user confirmation first (AGENT_GUIDE §13.5). Check for `OLLAMA_API_KEY` by name only: `test -n "$OLLAMA_API_KEY" && echo set`.

Two entry points run T4, and both write to the failure ledger:

- `uv run python -m regression.t4 --suite <name> --agent-url <url>` runs a suite and posts the scorecard. CI and AGENT_GUIDE §13.7 use it.
- `uv run pytest regression/t4 --live --agent-url <url> [--suite <name>]` runs the same scenarios as pytest items. Without `--live`, T4 items are deselected.

---

### P4-01 — Move the `evals/` code into `regression/t4/` and the selftests into `regression/selftest/`

- **Depends on:** P0B-07
- **Blocked by:** none
- **Read:** [07-migration-map.md](../reference/07-migration-map.md), every `evals/*` row; [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `fixtures/`, `t4/`, `t5/` (`trials.py` only) and `selftest/` subtrees, and §4.5; `.github/workflows/ci.yml`
- **Creates / changes:**
  - `git mv` `runner`, `scenario_loader`, `models`, `persistence`, `diff`, `report` and `eval_config.yaml` into `regression/t4/`; `git mv evals/scorer.py regression/t4/score.py`; `git mv evals/trials.py regression/t5/trials.py`;
  - `git mv evals/scenarios regression/t4/scenarios` and `git mv evals/suites regression/t4/suites`, **unconverted** (P4-03 converts them), so `EVAL_ROOT`-relative paths keep working;
  - `git mv evals/baselines/smoke.json regression/t4/baselines/smoke.json` (a `report.py` token/latency baseline, not a T5 baseline; `smoke.md` goes with it);
  - `regression/t4/__main__.py`, which calls `report.py`'s `main()` (that is where `--suite`, `--agent-url` and `--baseline` live);
  - `git mv evals/fixtures/coding/* regression/fixtures/workspaces/`; add `regression/fixtures/conftest.py` with `collect_ignore_glob = ["workspaces/*"]` (the fixture repos hold intentionally failing tests; today `evals/conftest.py` does this);
  - rewrite every `fixtures/coding/` reference, so that `rg -n 'fixtures/coding' regression evals tests` is empty. That covers T2 and T4 scenario YAML, and the paths in `test_assertions.py`, `test_report.py` and `test_coding_hard_oracle.py`;
  - `git mv` all seven `evals/test_*.py` into `regression/selftest/`. `test_assertions.py` merges into the one P0B-03 created; keep both sets of cases. `test_judge.py` is a network smoke test: mark it `live`, so it is deselected by default;
  - `scripts/ci-local.sh` and `.github/workflows/ci.yml`: delete the four `uv run python evals/test_*.py` lines (the selftests now run inside `pytest tests regression`);
  - delete `evals/assertions.py` and `evals/conftest.py` once nothing needs them.
- **Steps:**
  1. Move the files.
  2. Fix imports and paths.
  3. Turn any `if __name__ == "__main__"` self-check runner into plain pytest functions without changing what they assert.
- **Verify:**
  - `uv run pytest regression/selftest -q`: six selftests pass, and `test_judge` appears only with `--live`.
  - `uv run python -m regression.t4 --help`.
  - `uv run pytest tests regression -m "t1 or t2 or t3" --co -qq | rg -c 'workspaces/'` prints 0.
  - `bash scripts/ci-local.sh`.
- **Done when:** nothing in `tests/` or `regression/` imports from `evals`, and the Verify list holds.
- **Not moved here:** `evals/knowledge_layer/`, `evals/runs/`, `evals/monkeybot_evals.egg-info/` and `evals/TEST_COVERAGE.md` stay until P6-01 deletes them (§4.5). `evals/smoke_agent/` moves in P4-02.
- **Stop and ask if:** a selftest other than `test_judge.py` needs network access, or an `evals/` path is in neither the migration map nor the list above.

### P4-02 — Agent project, config split, judge config, preflight, ledger records

- **Depends on:** P4-01
- **Blocked by:** none. D12 is decided: `judge_model` is `glm-5.3-flash` on `ollama-cloud`, the same model as the agent.
- **Read:** [05.11-live-provider.md](../reference/05.11-live-provider.md) (all); [05.9-failure-ledger.md](../reference/05.9-failure-ledger.md) §5.9.3 (the `live` fields); [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t4/agent/` subtree only; [6U-gw.md](../reference/06-coverage/6U-gw.md), row T4-GW-01; [12-decisions.md](../reference/12-decisions.md), D12 only
- **Creates / changes:**
  - `git mv evals/smoke_agent regression/t4/agent`;
  - `git mv regression/t4/agent/fixture_mcp_server.py regression/fixtures/mcp/fixture_mcp_server.py`. Then point every reference at the new path: `rg -n 'fixture_mcp_server' tests regression`. That covers `tests/unit/core/mcp/test_mcp_client.py` (path constant only), the T2 MCP path from P1-14, and the agent's `mcp.json`, which becomes `${MONKEYBOT_AGENT_ROOT}/../../fixtures/mcp/fixture_mcp_server.py`;
  - split `monkeybot.verifier-on.yaml` into `monkeybot.verifier-tracker.yaml` and `monkeybot.verifier-judge.yaml`; add `mcp.progressive.json`;
  - `regression/t4/preflight.py`;
  - `regression/t4/eval_config.yaml`: `judge_provider: ollama-cloud`, `judge_model: glm-5.3-flash`;
  - ledger records from the runner (in `report.py` or `runner.py`): one `LedgerWriter` failure per failed scenario, with `tier: t4`, `case`, the `live` block (provider, model, agent URL) and `artifacts: [<run JSON path>]`;
  - T4-GW-01 (the global live invariant) checked by the runner on every live scenario, with a selftest using a recorded run JSON;
  - `.gitignore` entries for `regression/t4/agent/.env`, `data/`, `workspace/` and `regression/t4/runs/`.
- **Steps:**
  1. The preflight check runs before the first scenario of a live run (CLI or pytest `--live`), never at collection. It exits non-zero with the exact §5.11 message when the key is missing, and runs one cheap `stream` call to fail fast on a bad key or model.
  2. On a missing key, it writes one `run_error` ledger record.
  3. The runners never read, copy or print the key.
- **Verify:**
  - `env -u OLLAMA_API_KEY uv run python -m regression.t4 --suite smoke` fails with the §5.11 message and leaves a `run_error` record. No key is needed and nothing calls the network.
  - `uv run pytest tests/unit/core/mcp/test_mcp_client.py regression/selftest -q`.
- **Done when:** that check passes, the MCP fixture path works from its new location, and (with the user's confirmation) a preflight-only live run passes.

### P4-03 — Convert the 29 live scenarios and the suites

- **Depends on:** P4-02
- **Blocked by:** none
- **Read:** [07-migration-map.md](../reference/07-migration-map.md), the `evals/scenarios/**` and `evals/suites/**` rows; [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md), the `live` keys; [05.3-assertions-and-invariants.md](../reference/05.3-assertions-and-invariants.md) §5.3.1, the `live` rows; [04-file-structure.md](../reference/04-file-structure.md) §4.4, `t4/scenarios/` and `t4/suites/` only; for each scenario's case ID, the T4 rows of its area file (the IDs are in §4.4)
- **Creates / changes:**
  - converts the 29 YAML files in place under `regression/t4/scenarios/<area>/` (renaming the folders to the §4.4 names, for example `tools/` → `file/` and `shell/`), adding `case:`;
  - this task owns the retune of T4-CTX-01 and the extension of T4-SKILL-01 as their rows say;
  - rewrites the suite entries to the new paths (`tools/core_read` → `file/core_read`, and so on) and adds `file/core_read_error` to `smoke`;
  - `scenario_loader.py` becomes a thin wrapper over `regression/schema`;
  - `regression/plugin.py` collects `regression/t4/scenarios/**/*.yaml` as `t4` items. `--suite` filters by the suite file, and items run against `--agent-url`.
- **Notes:** in a range row (`T4-CODE-01..03`, `T4-CODE-04..10`, `T4-VER-01..05`), give each file one expanded ID in suite order (`T4-CODE-01`, `T4-CODE-02`, …) and list every file in the row's Test cell (AGENT_GUIDE §13.3).
- **Verify:** `uv run pytest regression/t4 --live --collect-only -qq | wc -l` prints 29. Collection alone must not start the agent, run the preflight check, or touch the network. `uv run pytest regression/selftest -q`.
- **Done when:** every moved scenario parses under the unified schema and has a case ID and a Test cell.

### P4-04 — New T4 cases and suites

- **Depends on:** P4-03
- **Blocked by:** none (T4-RT-01 stays deferred)
- **Read:** the T4 rows of the area files for the IDs in this phase's Summary (except T4-CTX-01 and T4-SKILL-01, which P4-03 owns), plus T4-ATT-01 ([6H-att.md](../reference/06-coverage/6H-att.md)), T4-CFG-01 ([6R-cfg.md](../reference/06-coverage/6R-cfg.md)), T4-PROV-01 ([6T-prov.md](../reference/06-coverage/6T-prov.md)) and T4-OPS-01 ([6W-ops.md](../reference/06-coverage/6W-ops.md)); [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t4/scenarios/`, `t4/suites/` and `t4/python/` lines marked `+`
- **Creates:**
  - the `+` scenario files under `regression/t4/scenarios/`;
  - `regression/t4/suites/{context,memory,mcp,scheduler,subagents,nightly}.yaml`;
  - `regression/t4/python/test_provider_live_matrix.py`, `regression/t4/python/test_docker_image_health.py`.
- **Notes:** this task only writes the cases; P4-06 runs them.
- **Verify:** `uv run pytest regression/t4 --live --collect-only -qq` collects the new items too; `uv run pytest tests/contracts/test_catalog_pairing.py -q` shows no T4 row without a Test cell except T4-RT-01.
- **Done when:** the Verify list holds.

### P4-05 — Live workflow and artifacts

- **Depends on:** P4-03
- **Blocked by:** none. D1 is decided (2026-10-26): do not add `OLLAMA_API_KEY` as a GitHub secret. The smoke suite runs locally in P4-06.
- **Read:** [09-ci.md](../reference/09-ci.md), the `live-smoke` row; [05.11-live-provider.md](../reference/05.11-live-provider.md), the table; `.github/workflows/live-eval-smoke.yml` (all)
- **Creates / changes:** `.github/workflows/live-eval-smoke.yml`. Keep its triggers. Do not add `OLLAMA_API_KEY`, `NVIDIA_API_KEY`, or any other provider key, and do not add a step that starts a live suite. Change:
  - remove the secret gate and every job env that passes a provider key;
  - `working-directory` and the lockfile trigger paths: `evals/smoke_agent` → `regression/t4/agent`;
  - `MONKEYBOT_AGENT_CONFIG: regression/t4/agent/monkeybot_config/monkeybot.yaml`.
  The scorecard and `test-runs/` come from the local smoke run in P4-06, not from this workflow.
- **Verify:** `uvx actionlint .github/workflows/live-eval-smoke.yml` if available, otherwise a careful read; `rg -n 'evals/|OLLAMA_API_KEY|NVIDIA_API_KEY|secrets\.' .github/workflows/live-eval-smoke.yml` finds nothing.
- **Done when:** the workflow references only new paths and requires no provider key.

### P4-06 — Live smoke; phase exit

- **Depends on:** P4-04, P4-05
- **Blocked by:** user confirmation (the local live run)
- **Read:** this phase file's Summary; `docs/live-evals.md` (how to start the agent gateway locally), with paths read as their new locations
- **Steps:**
  1. Start the agent from `regression/t4/agent` as `docs/live-evals.md` describes, then run `uv run python -m regression.t4 --suite smoke --agent-url http://127.0.0.1:8787` once.
  2. Report pass/fail per case, the scorecard, and the ledger path.
  3. Do not fix live failures; they are signal for Phase 5 baselines or for later harness work.
  4. If no case failed, prove the ledger path once: run one scenario with a deliberately impossible assertion through a temporary copy (deleted afterwards), and check that its record has provider, model and the run JSON path.
- **Done when:** the Summary exit criteria hold on the local smoke run, and every T4 case except T4-RT-01 has a Test cell. GitHub does not need `OLLAMA_API_KEY`.
