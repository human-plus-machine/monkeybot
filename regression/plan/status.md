# Status

> Part of the [regression plan](README.md). This is the only plan file agents edit while working. What each task is and what it depends on: [tasks.md](tasks.md). Rules: [AGENT_GUIDE.md](AGENT_GUIDE.md) §13.1, §13.3 and §13.8.

## How to update this file

- **Starting a task:** set its row to `in-progress`, fill Owner and Updated, add its ID to **In progress** under Current, and set the phase row to `in-progress` if it was `todo`. Do this before any other change.
- **Stopping without finishing:** leave the row `in-progress` (or `blocked`), and add a handoff entry saying what is left.
- **Finishing a task:** set it to `done` only when every **Done when** bullet in its phase file holds. Fill Ref (PR, commit, or `uncommitted`), add a handoff entry, remove the ID from **In progress**, set **Next task**, and add 1 to the phase row's Done count.
- **Finishing a phase:** a phase is `done` when its exit task is `done` (for Phases 2 and 3, which have no exit task, when all their tasks are `done` or `deferred`). Set Completed.
- **Decisions:** when the user resolves a decision, record it in [reference/12-decisions.md](reference/12-decisions.md) first, then update the Decisions table here.
- **Deferring a task:** only when the user says so. Set it to `deferred` and give the reason in Note.
- **Several agents at once:** re-read this file right before each edit and change only the lines named above (your task row, your ID in Current, one count in your phase row). Only the agent that finishes a phase's exit task sets that phase to `done`. Never delete or rewrite someone else's row or entry.

Status values (same for tasks and phases): `todo`, `in-progress`, `blocked`, `done`, `deferred`. Owner is the agent or session name; Updated is a date (YYYY-MM-DD).

## Current

- **In progress:** none
- **Last update:** 2026-10-25, P0B-07 done
- **Next task:** P1-01

## Phases

| Phase | Name | Tasks | Done | Status | Exit check | Completed |
|---|---|---|---|---|---|---|
| [0a](phases/phase-0a-tier1-reorg.md) | Tier-1 reorganization and failure ledger | 6 | 6 | done | P0A-06 | 2026-10-25 |
| [0b](phases/phase-0b-foundations.md) | Foundations and docs/testing.md | 7 | 7 | done | P0B-07 | 2026-10-25 |
| [1](phases/phase-1-t2-harness-logic.md) | T2 harness logic | 18 | 0 | todo | P1-18 | |
| [2](phases/phase-2-contracts-and-gateway.md) | Contracts and gateway | 10 | 0 | todo | all P2 tasks done | |
| [3](phases/phase-3-record-replay.md) | Record / replay | 4 | 0 | todo | all P3 tasks done | |
| [4](phases/phase-4-live.md) | Live tier | 6 | 0 | todo | P4-06 | |
| [5](phases/phase-5-trials.md) | Trials and baselines | 3 | 0 | todo | P5-03 | |
| [6](phases/phase-6-cutover.md) | Cutover | 1 | 0 | todo | P6-01 | |

## Tasks

| ID | Status | Owner | Updated | Ref | Note |
|---|---|---|---|---|---|
| P0A-01 | done | cursor | 2026-10-25 | #335 | baseline 3320 collected, 3295 passed, 25 skipped |
| P0A-02 | done | cursor | 2026-10-25 | #336 | postToolUseFailure added; live notice confirmed |
| P0A-03 | done | cursor | 2026-10-25 | #336 | helpers in testkit; differing doubles kept for D7 |
| P0A-04 | done | cursor | 2026-10-25 | #336 | tier-1 tests in tests/unit; observability has no __init__.py |
| P0A-05 | done | cursor | 2026-10-25 | #336 | integration and acceptance tests are t2; ci-local.sh passes |
| P0A-06 | done | cursor | 2026-10-25 | #336 | empty test dirs removed; exit checks hold |
| P0B-01 | done | cursor | 2026-10-25 | uncommitted | strict multi-turn ScriptedProvider; fake script env |
| P0B-02 | done | cursor | 2026-10-25 | uncommitted | scenario models; 104 selftests passed |
| P0B-03 | done | cursor | 2026-10-25 | uncommitted | assertion keys and 8 invariants; 151 selftests |
| P0B-04 | done | cursor | 2026-10-25 | uncommitted | loop driver and YAML collector; throwaway scenario deleted |
| P0B-05 | done | cursor | 2026-10-25 | uncommitted | 19 scenarios migrated; error codes read from ToolCallResult.error |
| P0B-06 | done | cursor | 2026-10-25 | uncommitted | 419 catalog rows; pairing test; prompt prints Expect |
| P0B-07 | done | cursor | 2026-10-25 | uncommitted | root conftest loads otel fixtures; gate under 2 min |
| P1-01 | todo | | | | |
| P1-02 | todo | | | | |
| P1-03 | todo | | | | |
| P1-04 | todo | | | | |
| P1-05 | todo | | | | |
| P1-06 | todo | | | | |
| P1-07 | todo | | | | |
| P1-08 | todo | | | | |
| P1-09 | todo | | | | |
| P1-10 | todo | | | | |
| P1-11 | todo | | | | |
| P1-12 | todo | | | | |
| P1-13 | todo | | | | |
| P1-14 | todo | | | | |
| P1-15 | todo | | | | |
| P1-16 | todo | | | | |
| P1-17 | todo | | | | |
| P1-18 | todo | | | | |
| P2-01 | todo | | | | |
| P2-02 | todo | | | | |
| P2-03 | todo | | | | |
| P2-04 | todo | | | | |
| P2-05 | todo | | | | |
| P2-06 | todo | | | | |
| P2-07 | todo | | | | |
| P2-08 | todo | | | | |
| P2-09 | todo | | | | |
| P2-10 | todo | | | | |
| P3-01 | todo | | | | |
| P3-02 | todo | | | | |
| P3-03 | todo | | | | |
| P3-04 | todo | | | | |
| P4-01 | todo | | | | |
| P4-02 | todo | | | | |
| P4-03 | todo | | | | |
| P4-04 | todo | | | | |
| P4-05 | todo | | | | |
| P4-06 | todo | | | | |
| P5-01 | todo | | | | |
| P5-02 | todo | | | | |
| P5-03 | todo | | | | |
| P6-01 | todo | | | | |

## Decisions

Only the user resolves these. Full text: [reference/12-decisions.md](reference/12-decisions.md).

| Decision | State | Blocks |
|---|---|---|
| D1: `OLLAMA_API_KEY` as a GitHub secret | open | CI runs in P4-05, P4-06, P5-03 |
| D3: Postgres / Firestore in CI | open | non-sqlite part of P2-07 |
| D4: macOS runner for `COMP` | open | CI runs of the P1-17 `COMP` cases |
| D12: judge model on Ollama Cloud | open | `judge_model` in P4-02; judged suites in P5-02 |
| D13: wire cassettes for non-Ollama providers | open | T3-PROV-01..05 and the non-Ollama families of T3-PROV-06, in P3-03 |

These have no blocking effect; the plan follows the recommendation in 12-decisions.md until the user says otherwise. If the user overrides one, stop and ask which tasks to revisit.

| Decision | State |
|---|---|
| D2: run `ci.yml` on pull requests | open (nothing waits on it; CI stays manual plus pre-push) |
| D5: where shared doubles live | assumed: (c), `testkit/` plus the seams in `src/monkeybot/core/testing/` |
| D6: `regression/` packaging | assumed: root project; `jsonschema` in the existing `dev` group via `uv add --dev` (installed by default); `deepeval` and `langfuse` stay in the `evals` extra |
| D7: dedupe unit vs contract tests | assumed: keep both through Phase 6 |
| D8: tier-1 layout | assumed: (a), `tests/unit` + `tests/contracts` |
| D9: enforcing "do not auto-fix" | assumed: (a), banner + rule + advisory hook |
| D10: where the agent rule lives | assumed: `.cursor/rules` only |
| D11: ledger retention | assumed: newest 200 runs or 30 days, whichever keeps more |

## Handoff log

Append one entry per stop or finish, newest at the bottom. Use this shape (copied from the AGENT_GUIDE §13.8 report):

```text
### <date> <task id> <owner>
Task:      <task id and title>
Status:    done | in-progress | blocked (<reason>)
Changed:   <files, grouped>
Verify:    <command → summary line>
Catalog:   <rows filled in docs/testing.md, or "n/a before P0B-06">
Recorded failures not fixed: <run file + case id, path, one-line error each, or none>
Open questions: <or none>
Next:      <next task id>
```

### 2026-10-25 P0A-01 cursor
Task:      P0A-01 Failure ledger: plugin, scrubber, triage CLI
Status:    done
Changed:   testkit/ (scrub.py, failure_ledger.py, failures CLI), tests/unit/testkit/ (23 tests), pytest.ini addopts, .gitignore /test-runs/; baseline in test-runs/phase0a/ (gitignored)
Verify:    pytest tests/unit/testkit -q → 23 passed in 127.89s; pytest tests -q -n auto → 3318 passed, 25 skipped (baseline 3295 passed, 25 skipped, plus 23 new); python -m testkit.failures latest → complete=True failures=0; mypy testkit → success; ruff format --check . → 681 files already formatted
Catalog:   n/a before P0B-06
Recorded failures not fixed: none
Open questions: none
Next:      P0A-02 (P0A-03 can run in parallel)

### 2026-10-25 P0A-02 cursor
Task:      P0A-02 Agent rule and Cursor hook
Status:    in-progress (samples pass; waiting on a live hook confirmation)
Changed:   .cursor/rules/test-failures.mdc; .cursor/hooks/test-failure-notice.py; .cursor/hooks.json (postToolUse Shell; both pre-pr.py entries unchanged); .gitignore (/.cursor/hooks/.state/)
Verify:    ruff format --check and ruff check on the hook → passed. Piped payloads: failing pytest → additional_context with run path; same run again → nothing; passing pytest → nothing; non-test command while LATEST has failures → nothing. Live postToolUse payload field is tool_input.command.
Catalog:   n/a before P0B-06
Recorded failures not fixed: none
Open questions: user still needs to run a failing test in an agent session and confirm the notice in the Hooks output channel
Next:      P0A-02 confirmation, then P0A-03 (can run in parallel)

### 2026-10-25 P0A-02 cursor
Task:      P0A-02 Agent rule and Cursor hook
Status:    blocked (live notice does not fire on a failing shell)
Changed:   no further file edits; deleted the throwaway probe test after the run
Verify:    in-repo failing pytest → exit 1, ledger banner in the command output, Hooks channel has beforeShellExecution only (no postToolUse). Later `uv run pytest --version` (exit 0) → Hooks OUTPUT additional_context, and that text was injected into the agent turn. Probe file removed.
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T200732Z-76ae692-17786.jsonl, case null, test-runs/test_p0a02_hook_probe.py, AssertionError: P0A-02 hook probe (probe file deleted)
Open questions: add the same script on postToolUseFailure, or leave postToolUse only?
Next:      P0A-02 once the user answers

### 2026-10-25 P0A-02 cursor
Task:      P0A-02 Agent rule and Cursor hook
Status:    done
Changed:   .cursor/hooks.json (same script also on postToolUseFailure / Shell; pre-pr.py entries unchanged)
Verify:    failing in-repo pytest exit 1 → Hooks channel postToolUseFailure OUTPUT additional_context for test-runs/20261025T200904Z-76ae692-23311.jsonl, and that notice was injected into the agent turn. Command field remains tool_input.command. Probe file deleted.
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T200904Z-76ae692-23311.jsonl, case null, test-runs/test_p0a02_hook_probe.py, AssertionError: P0A-02 hook probe (probe file deleted)
Open questions: none
Next:      P0A-03

### 2026-10-25 P0A-03 cursor
Task:      P0A-03 Move shared helpers into testkit
Status:    done
Changed:   git mv tests/core/memory/helpers.py → testkit/memory.py and in_memory_palace.py → testkit/palace.py. New modules: testkit/{provider,history,executors,inspectors,context,mcp,hooks,verifier,sandbox,conversations,gateway_ports}.py and tests/unit/testkit/test_fake_history.py. Importers under tests/ now use testkit; the two tests.evals imports inside tests/evals/ stay. pending_bus.py was not created (PresetPendingBus and CountingFakeProvider stay until P1-04). D7, kept separate: HookFakeHistory vs FakeHistory (limit, preload, reset_calls); trace_mem_sub vs mem_sub (palace inside vs outside the workspace); TraceNoMCP vs NoMCP; verifier_payload vs hooks _payload; local FakeHistory/executor/provider copies in test_realtime_loop.py and test_loop_transcript.py; local _make_opensandbox_module in test_core_tool_executor.py. After the suite, a concurrent edit (not this task) changed src/monkeybot/core/runtime/doom_loop.py, tool_dispatch.py, and added two tests in tests/core/test_doom_loop.py. Left those in place.
Verify:    rg of from|import tests. outside tests/evals/ → nothing. mypy testkit → no issues in 19 files. ruff check testkit and tests/unit/testkit → passed. pytest tests -q -n auto → 3321 passed, 25 skipped in 69.13s (baseline 3295 passed, 25 skipped; plus 26 testkit tests). Collected 3346 = baseline 3320 + 26. Every baseline PASSED nodeid still passed; the same skip reasons printed. tests/core/test_doom_loop.py after the concurrent edit → 32 passed.
Catalog:   n/a before P0B-06
Recorded failures not fixed: none
Open questions: none
Next:      P0A-04

### 2026-10-25 P0A-04 cursor
Task:      P0A-04 git mv tier-1 tests into tests/unit/
Status:    done
Changed:   206 git mv into tests/unit/ (116 tests/core files plus the fixture, the other area trees, and the three packaging tests). __init__.py added in every new folder except tests/unit/observability: that folder is also loaded from pytest_plugins, and an __init__.py makes pytest 9 register the same conftest twice. tests/conftest.py pytest_plugins path is now tests.unit.observability.conftest. parents[N] and the bitbucket fixture path adjusted for the new depth. tests/evals, tests/integration, and tests/acceptance stayed. Empty old directories remain for P0A-06.
Verify:    pytest tests --co -qq → 3346 collected. Reverse the move map onto those IDs → 3320, identical to before_ids.txt; the other 26 are tests/unit/testkit. pytest tests -q -n auto → 3321 passed, 25 skipped in 64.25s (same as after P0A-03). git diff --cached shows the moves as renames. git log --follow on the new paths is empty until the rename is committed; the pre-move paths still show history (test_loop.py c35e0d5b, test_cold_start.py dca52adf, bitbucket_dc_style_pr.diff 28f35488).
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T202832Z-76ae692-96838.jsonl, case null, tests/unit/observability, ValueError: Plugin already registered under a different name (collection, before tests/unit/observability/__init__.py was removed). The full run after that removal had no failures.
Open questions: none
Next:      P0A-05

### 2026-10-25 P0A-05 cursor
Task:      P0A-05 Integration and acceptance tests into regression/; config and scripts
Status:    done
Changed:   git mv the three integration tests and the acceptance test to regression/t2/python/ (test_mb_e2e_simple_reply.py renamed test_e2e_simple_reply.py). Their integration/acceptance marks are t2. parents[2] is parents[3] in the two observability files. regression/__init__.py, regression/t2/__init__.py, regression/t2/python/__init__.py. tests/unit/conftest.py marks tests/unit t1. regression/conftest.py marks t1–t5 and selftest by directory. tests/conftest.py marks tests/evals t2. pytest.ini testpaths and markers. pre-commit files regex and precommit-pytest.sh collection fallback. ci-local.sh exports MONKEYBOT_TEST_TRIGGER=pre-push and a shared MONKEYBOT_TEST_RUN_GROUP, runs pytest tests regression -m "t1 or t2 or t3", mypy src/monkeybot testkit, and passes the ledger plugin into cli/ and browser-mcp. pyproject mypy files and ruff known-first-party. ci.yml pytest and mypy match; evals self-check untouched. testkit/context.py and testkit/provider.py got type annotations so that combined mypy command passes; behavior unchanged. Four unit tests still carry @pytest.mark.integration, which is now an unknown-mark warning; they still run as t1.
Verify:    pytest -m t1 --co -qq → 3312/3346 collected (34 deselected). wc -l of that output is 3337 because of the summary and warnings. 3312 = 3346 minus 14 items in the four moved files minus 20 tests/evals items, which step 2 marks t2. pytest -m t2 --co -q lists the four regression/t2/python modules (14 items) plus the 20 eval items. bash scripts/ci-local.sh → exit 0 (mypy 279 files, pytest tests regression exit 0 on 3346 items, cli 357 passed, browser-mcp 320 passed / 18 skipped, eval self-checks passed).
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T203702Z-76ae692-30364.jsonl, case null, 20 failures from a sandboxed ci-local.sh (git init and sandbox-exec Operation not permitted): tests/unit/core/mcp/test_mcp_client.py::test_disconnect_from_another_task_reaps_stdio_child; test_cancelled_connect_reaps_stdio_child; tests/unit/core/context/test_project_context.py::test_git_status_note_on_a_real_repo; test_git_status_note_uses_the_workspace_when_git_dir_is_set; tests/unit/core/subagents/test_worker_pool.py::test_direct_child_pids_finds_real_child; 13 TestMacJailBootstrap cases in tests/unit/core/tools/test_fs_isolation.py; tests/unit/core/test_bootstrap.py::test_cancelled_harness_startup_reaps_background_mcp. The same script outside the sandbox exited 0. Not edited.
Open questions: none
Next:      P0A-06

### 2026-10-25 P0A-06 cursor
Task:      P0A-06 Delete emptied folders and re-run the phase exit checks
Status:    done
Changed:   git rm tests/integration/__init__.py. Removed the emptied directories tests/{core,gateway,providers,observability,computer,scheduler,todo_list,web_search,cli,skills,integration,acceptance,profiling}. tests/evals and tests/unit remain. No test bodies edited.
Verify:    pytest tests regression --co -qq → 3346 collected. Reverse the 212 staged renames onto those IDs → 3320, identical to before_ids.txt; the other 26 are tests/unit/testkit. pytest tests regression -q -n auto -rs → 3321 passed, 25 skipped in 64.23s. The 25 SKIPPED lines match before_results.txt after the rename map, same file lines and reasons. bash scripts/ci-local.sh → exit 0 (ruff 723 files, mypy 279 files, pytest exit 0, cli 357 passed, browser-mcp 320 passed / 18 skipped, eval self-checks passed). git diff --cached --name-status -M shows R100 for the moved files, including tests/core/test_loop.py → tests/unit/core/runtime/test_loop.py. git log --follow on the pre-move path prints c35e0d5b; the new path prints nothing until that rename is committed. Deliberate failing pytest of test-runs/test_p0a06_hook_probe.py → exit 1, ledger banner, and postToolUseFailure additional_context injected into the turn. Probe file deleted. rg of tests/core/ or from tests. outside the excluded globs hits only tests/unit/core/fixtures/bitbucket_dc_style_pr.diff (captured diff bytes, lines 274, 276, 277, 406, 408, 409). Left that file, and the expected hits in docs/, src/, evals/, and tests/evals/, for P6-01.
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T204513Z-76ae692-86445.jsonl, case null, test-runs/test_p0a06_hook_probe.py, AssertionError: P0A-06 hook probe (probe deleted). test-runs/20261025T204543Z-76ae692-88316.jsonl, case null, test-runs/test_p0a06_hook_probe.py, AssertionError: P0A-06 hook probe (probe deleted; this is the run whose shell exit was 1 and whose notice came from postToolUseFailure).
Open questions: none
Next:      P0B-01

### 2026-10-25 P0B-02 cursor
Task:      P0B-02 Scenario schema (models only)
Status:    done
Changed:   regression/schema/{__init__,scenario,events,steps}.py; regression/selftest/{__init__,test_scenario_schema}.py
Verify:    uv run pytest regression/selftest -q → collected 104, 104 passed in 0.17s. uv run mypy testkit regression/schema → 1 error, testkit/context.py:80 unused type: ignore (pre-existing; file not edited). regression/schema reported no mypy errors.
Catalog:   n/a before P0B-06
Recorded failures not fixed: none
Open questions: none
Next:      P0B-03

### 2026-10-25 P0B-01 cursor
Task:      P0B-01 Multi-turn strict ScriptedProvider
Status:    done
Changed:   src/monkeybot/core/testing/mocks_provider.py (ScriptedProvider, RaiseStep, YAML/JSON loader; ScriptedFakeProvider unchanged). testkit/provider.py re-exports ScriptedProvider. tests/unit/testkit/test_scripted_provider.py. Extra, step 5: src/monkeybot/gateway/sse/app.py and src/monkeybot/core/subagents/subagent_worker.py call scripted_provider_from_env when MONKEYBOT_FAKE_PROVIDER_SCRIPT is set; unset still uses MONKEYBOT_FAKE_PROVIDER_EVENTS and the existing defaults. The script path is not an ENV_MAP key, so a snapshot without it falls through to the process environment.
Verify:    uv run pytest tests/unit/testkit tests/unit/core/testing -q → 48 passed in 124.00s. uv run pytest tests -q -n auto → 3324 passed, 25 skipped in 78.42s. uv run mypy src/monkeybot testkit → Success: no issues found in 279 source files.
Catalog:   n/a before P0B-06
Recorded failures not fixed: none
Open questions: none
Next:      P0B-04 waits on P0B-03 (P0B-02 is already done; P0B-03 is in progress)

### 2026-10-25 P0B-03 cursor
Task:      P0B-03 Assertion engine and global invariants
Status:    done
Changed:   regression/schema/assertions.py, regression/schema/invariants.py, regression/selftest/test_assertions.py, regression/selftest/test_invariants.py. regression/schema/scenario.py calls validate_assertions at the end of the mode validator so wrong-mode keys and verifier_on/verifier_off with EVAL_VERIFIER_MODE unset fail at load; assertions stay stored unparsed. evals/assertions.py was not edited. tests/contracts/goldens/sse/schema.json does not exist; sse_schema reads that path and fails closed, and the selftests pass a temporary schema instead of inventing one.
Verify:    uv run pytest regression/selftest -q → 151 passed in 0.28s (the previous 104 schema tests still pass).
Catalog:   n/a before P0B-06
Recorded failures not fixed: none
Open questions: none
Next:      P0B-04

### 2026-10-25 P0B-04 cursor
Task:      P0B-04 Loop driver, record hook, YAML collector
Status:    done
Changed:   regression/plugin.py (options and t2 YAML collection). regression/conftest.py (T4/T5 deselected unless --live; tier marks kept). pytest.ini addopts adds -p regression.plugin. regression/drivers/{__init__.py,loop.py}. git mv tests/evals/eval_hook.py → regression/drivers/record.py. tests/evals/scenario_runner.py import path only. regression/fixtures/configs/agent.base.yaml. Throwaway regression/t2/loop/scenarios/p0b04_probe.yaml created, run, and deleted.
Verify:    uv run pytest regression -k p0b04_probe --collect-only → node id regression/t2/loop/scenarios/p0b04_probe.yaml::p0b04_probe; 166 collected, 165 deselected, 1 selected; -m t2 collected that item. uv run pytest regression -k p0b04_probe -n auto -q → 14 workers [1 item], 1 passed in 7.36s. Broken assertion plus a leftover script turn: uv run pytest regression/t2/loop/scenarios/p0b04_probe.yaml -n auto -q → 1 failed in 7.32s; strict_script and response_contains both fired. Ledger test-runs/20261025T221458Z-d57f399-61565.jsonl has tier t2 and case T2-LOOP-P0B04. Probe file deleted. uv run pytest regression -q → 164 passed, 1 error in 1.37s (151 selftests passed). uv run pytest tests/evals --co -q → 20 collected. CI=true uv run pytest regression/selftest --co -q --update-golden → exit 4, "--update-golden is refused when CI is set".
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T221458Z-d57f399-61565.jsonl, case T2-LOOP-P0B04, regression/t2/loop/scenarios/p0b04_probe.yaml::p0b04_probe, strict_script leftover plus response_contains 'not-in-the-reply' (throwaway deleted; assertion not weakened). test-runs/20261025T221539Z-d57f399-64137.jsonl, case null, regression/t2/python/test_observability_integration.py::test_integrated_run_task_subagent_shares_parent_trace_id, fixture 'otel_memory_exporter' not found. That fixture is registered from tests/conftest.py, which a regression-only run does not load. Not edited.
Open questions: none
Next:      P0B-05

### 2026-10-25 P0B-05 cursor
Task:      P0B-05 Migrate the 19 tests/evals scenarios
Status:    blocked (T2-CODE-01 tool_error_codes cannot see details.code)
Changed:   git mv of the 19 tests/evals/scenarios/*.yaml files into regression/t2/loop/scenarios/<area>/ with the §4.4 names, then converted to the unified schema (messages, script, memory.index, hooks: [verification_gate], case, mode: scripted, driver: loop, text_delta → text). Executors left as they were. max_turns dropped (not a legal assertion key; it was a loop cap, not a checked assertion). memory_injected: true became memory_injected.contains of the seeded line. tests/evals/scenarios/ removed. tests/evals/test_agent_behavior.py and tests/evals/scenario_runner.py not deleted. tests/evals/test_verifier_port.py not moved.
Verify:    uv run pytest regression/t2/loop -q → 1 failed, 18 passed in 43.28s. The 18 passed with invariants on and no invariants_skip. Did not run uv run pytest regression/t2 -q and did not mark done.
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T222328Z-d57f399-92360.jsonl, case T2-CODE-01, regression/t2/loop/scenarios/coding/read_before_write.yaml::read_before_write, tool_error_codes: missing ['read_before_write'] (saw []). The first replace_in_file event has that code in ToolCallResult.error and result ''. _result_failed only parses result. The old scenario_runner parsed result.error. Workspace assertions on the same run passed. Prior, not re-run and not fixed: test-runs/20261025T221539Z-d57f399-64137.jsonl, case null, regression/t2/python/test_observability_integration.py::test_integrated_run_task_subagent_shares_parent_trace_id, fixture 'otel_memory_exporter' not found.
Open questions: May P0B-05 teach regression/schema/assertions.py _result_failed to take details.code from a JSON envelope on ToolCallResult.error when result has none? That file is outside this task's list. The alternative is to leave T2-CODE-01 failing. I did not add invariants_skip or script_partial, and I did not change the assertion.
Next:      P0B-06 once unblocked; status.md Next task stays P0B-05

### 2026-10-25 P0B-05 cursor
Task:      P0B-05 Migrate the 19 tests/evals scenarios
Status:    done
Changed:   regression/schema/assertions.py (_result_failed reads details.code from a JSON envelope on ToolCallResult.error when the result body has none; a result-body code still wins; a non-JSON error string is still a failure and adds no code). regression/selftest/test_assertions.py (code only on the error field; non-JSON error invents no code). 19 YAML files under regression/t2/loop/scenarios/<area>/ via git mv from tests/evals/scenarios/, converted to the unified schema. Deleted tests/evals/test_agent_behavior.py and tests/evals/scenario_runner.py. tests/evals/test_verifier_port.py stays. tests/evals/scenarios/ removed.
Verify:    uv run pytest regression/selftest -q → 153 passed in 0.28s. uv run pytest regression/t2/loop -q → 19 passed in 43.24s, invariants on, no invariants_skip. uv run pytest regression/t2 -q → 32 passed, 1 error in 44.27s.
Catalog:   n/a before P0B-06
Recorded failures not fixed: test-runs/20261025T223103Z-d57f399-19977.jsonl, case null, regression/t2/python/test_observability_integration.py::test_integrated_run_task_subagent_shares_parent_trace_id, fixture 'otel_memory_exporter' not found. That fixture is registered from tests/conftest.py, which a regression-only pytest does not load. Not edited.
Open questions: none
Next:      P0B-06

### 2026-10-25 P0B-06 cursor
Task:      P0B-06 docs/testing.md, catalog parser, pairing test
Status:    done
Changed:   docs/testing.md (sections 1–9; §8 is 419 rows). testkit/failures/catalog.py (stdlib parser). testkit/failure_ledger.py and testkit/failures/__main__.py (catalog_expect on the record and on prompt). tests/contracts/{__init__,conftest,test_catalog_pairing}.py. tests/unit/testkit/test_failures_cli.py (prompt prints Expect). tests/unit/testkit/test_failure_ledger.py (T2-LOOP-07 catalog_expect is the catalog sentence; the old assertion was None). The 19 migrated scenarios already had case:. Throwaway generator deleted.
Verify:    uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed in 0.68s; uncovered total: 401 of 419. Bogus ID: turn_completion.yaml case set to T2-LOOP-99 → FAILED, AssertionError: regression/t2/loop/scenarios/loop/turn_completion.yaml cites T2-LOOP-99, which is not in the catalog (test-runs/20261025T225641Z-d57f399-12105.jsonl). Restored case: T2-LOOP-01; pairing re-run 1 passed. uv run pytest tests/unit/testkit -q → 44 passed in 124.25s. mypy on catalog.py, __main__.py, failure_ledger.py → success.
Catalog:   419 §8 rows. 18 Test cells filled, covering all 19 migrated files (T2-TODO-01 lists both todo scenarios). T1-STORE-02, T1-STORE-09, T1-STORE-10, and T1-STORE-11 have an empty Expect because the §6 Asserts cell was empty.
Recorded failures not fixed: test-runs/20261025T225641Z-d57f399-12105.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: turn_completion.yaml cites T2-LOOP-99 (deliberate probe; case restored to T2-LOOP-01). Prior, not re-run and not fixed: test-runs/20261025T223103Z-d57f399-19977.jsonl, case null, regression/t2/python/test_observability_integration.py::test_integrated_run_task_subagent_shares_parent_trace_id, fixture 'otel_memory_exporter' not found.
Open questions: none
Next:      P0B-07

### 2026-10-25 P0B-07 cursor
Task:      P0B-07 Wire regression into the gates; phase exit
Status:    done
Changed:   conftest.py (new; pytest_plugins loads tests.unit.observability.conftest when opentelemetry is installed). tests/conftest.py (that pytest_plugins line removed). scripts/ci-local.sh, .pre-commit-config.yaml, and scripts/precommit-pytest.sh already included regression; marker expression unchanged.
Verify:    uv run pytest regression/t2/loop -q → 19 passed in 43.46s; no invariants_skip in those scenarios. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed in 0.71s; uncovered total: 401 of 419. time uv run pytest regression -m "t1 or t2 or t3" -q -n auto → 186 passed in 17.49s, 21.224s total, exit 0. bash scripts/ci-local.sh → exit 0. Inside it, pytest tests regression -m "t1 or t2 or t3" -q -n auto collected 3518 items and exited 0 (ledger 20261025T231606Z, 23:16:06Z–23:17:55Z); the same command printed 3493 passed, 25 skipped, 71 warnings in 109.12s. No "Plugin already registered". cli pytest 357 passed in 25.13s; browser-mcp 320 passed, 18 skipped in 0.98s.
Catalog:   pairing test green; uncovered total 401 of 419; no catalog edits
Recorded failures not fixed: none
Open questions: none
Next:      P1-01

