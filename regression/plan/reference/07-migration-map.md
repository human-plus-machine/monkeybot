# 7. Migration map

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


| From | To | Action |
|---|---|---|
| `tests/evals/scenario_runner.py` | `regression/drivers/loop.py` + `regression/schema/` | Rewrite on the unified schema; keep behavior. |
| `tests/evals/eval_hook.py` | `regression/drivers/record.py` | Move; extend to collect verdicts, `triggering_signals`, subagent events. |
| `tests/evals/scenarios/*.yaml` (19) | `regression/t2/loop/scenarios/<area>/` | Convert keys (`user_message` → `messages`, `scripted_turns` → `script`, `memory_index` → `memory.index`, `gate: true` → `hooks: [verification_gate]`). Recording-only ones switch to the real executor where the matrix says so. |
| `tests/evals/test_agent_behavior.py`, `test_verifier_port.py` | `regression/t2/` collector; `T2-VER-*` | Replace. |
| `tests/core/test_loop.py`, `test_core_tool_executor.py`, `test_hooks.py`, `test_sandbox_executor.py` helpers; `tests/core/memory/helpers.py`, `in_memory_palace.py`; `tests/providers/conftest.py` builders | `testkit/*` + `src/monkeybot/core/testing/mocks_provider.py` | Move once; every importer (tier 1 and regression) switches to `testkit`. Duplicate copies collapse into one (section 5.1). |
| `tests/core/**` (116 test files) | `tests/unit/core/<package>/` | `git mv`, grouped by the `src/monkeybot/core/` subpackage they test (section 4.3). |
| `tests/{gateway,providers,observability,computer,scheduler,todo_list,web_search,cli,skills}/**` | `tests/unit/<same path>/` | `git mv`. |
| `tests/test_cold_start.py`, `test_packaging_config.py`, `test_release_script.py` | `tests/unit/packaging/` | `git mv`. |
| `tests/integration/*`, `tests/acceptance/*` | `regression/t2/python/` | `git mv`; markers `integration`/`acceptance` → `t2`. |
| `evals/runner.py`, `scorer.py`, `report.py`, `persistence.py`, `diff.py`, `models.py`, `scenario_loader.py` | `regression/t4/` | Move into a real package; remove `sys.path` hacks. Keep full tool args in run JSON. |
| `evals/assertions.py` | `regression/schema/assertions.py` | Merge with the T2 assertions; one implementation. |
| `evals/trials.py` | `regression/t5/trials.py` | Move; add Wilson intervals and keyed baselines. |
| `evals/scenarios/**` (29) | `regression/t4/scenarios/**` | Convert to unified schema. |
| `evals/suites/*.yaml` | `regression/t4/suites/` | Move; `smoke` adds `tools/core_read_error`. |
| `evals/fixtures/**` | `regression/fixtures/workspaces/**` | Move. |
| `evals/smoke_agent/**` | `regression/t4/agent/**` | Move; `workspace/` stays gitignored. Split verifier configs (tracker vs judge). `fixture_mcp_server.py` goes to `regression/fixtures/mcp/` so T2 can use it too. |
| `evals/baselines/smoke.*` | `regression/t4/baselines/smoke.*` | `git mv` unchanged (P4-01). It is the T4 report's token/latency baseline; T5 baselines are new files written in P5-02. |
| `evals/test_*.py` (scripts) | `regression/selftest/` | Convert to real pytest; `test_diff.py` and `test_scorer.py` start running. `test_judge.py` becomes a `live` test. |
| `evals/TEST_COVERAGE.md` | `regression/plan/reference/06-coverage/`, then `docs/testing.md` §8 | Delete after migration. |
| `docs/live-evals.md` | Rewrite to point at `regression/` | Fix stale claims (section 11). |
| `.github/workflows/live-eval-smoke.yml` | Same workflow, new paths | Drop the `NVIDIA_API_KEY` secret gate. Do not add `OLLAMA_API_KEY` (D1). The workflow does not start a live suite. Live smoke runs locally, with `JUDGE_PROVIDER: ollama-cloud` and `JUDGE_MODEL: glm-5.3-flash`. |

During migration, `evals/` and `tests/evals/` keep working. Delete them only after phase 6's exit criteria. The tier-1 moves happen earlier (Phase 0a) as one mechanical change, so later phases only ever touch the new paths.
