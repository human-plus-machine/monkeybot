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
- **Last update:** 2026-10-26, P1-18 done: STORE and OPS pass; phase 1 closed
- **Next task:** Phase 2

## Phases

| Phase | Name | Tasks | Done | Status | Exit check | Completed |
|---|---|---|---|---|---|---|
| [0a](phases/phase-0a-tier1-reorg.md) | Tier-1 reorganization and failure ledger | 6 | 6 | done | P0A-06 | 2026-10-25 |
| [0b](phases/phase-0b-foundations.md) | Foundations and docs/testing.md | 7 | 7 | done | P0B-07 | 2026-10-25 |
| [1](phases/phase-1-t2-harness-logic.md) | T2 harness logic | 18 | 18 | done | P1-18 | 2026-10-26 |
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
| P0B-01 | done | cursor | 2026-10-25 | #337 | strict multi-turn ScriptedProvider; fake script env |
| P0B-02 | done | cursor | 2026-10-25 | #337 | scenario models; 104 selftests passed |
| P0B-03 | done | cursor | 2026-10-25 | #337 | assertion keys and 8 invariants; 151 selftests |
| P0B-04 | done | cursor | 2026-10-25 | #337 | loop driver and YAML collector; throwaway scenario deleted |
| P0B-05 | done | cursor | 2026-10-25 | #337 | 19 scenarios migrated; error codes read from ToolCallResult.error |
| P0B-06 | done | cursor | 2026-10-25 | #337 | 419 catalog rows; pairing test; prompt prints Expect |
| P0B-07 | done | cursor | 2026-10-25 | #337 | root conftest loads otel fixtures; gate under 2 min |
| P1-01 | done | cursor | 2026-10-25 | uncommitted | 18 fixture selftests; secret.txt and pkg/hidden.txt need git add -f |
| P1-02 | done | cursor | 2026-10-25 | uncommitted | FakeClock drives scheduler, background summary, and stale claims |
| P1-03 | done | cursor | 2026-10-25 | uncommitted | 15 step tests; PresetPendingBus in testkit |
| P1-04 | done | cursor | 2026-10-26 | uncommitted | T2-LOOP-02..40 pass; job check, tool blocks, usage, and thinking budget are observed |
| P1-05 | done | cursor | 2026-10-26 | uncommitted | T2-FILE cases pass; `..` spills under `_`; mid-apply failure is `rollback_failed` |
| P1-06 | done | cursor | 2026-10-26 | uncommitted | T2-SHELL cases pass; nested shell is policy; `cwd: ../` is `invalid_cwd` |
| P1-07 | done | cursor | 2026-10-26 | uncommitted | T2-POL-01..11 pass; a granted `.env` is `credential_denied` |
| P1-08 | done | cursor | 2026-10-26 | uncommitted | T2-CODE-05..18 pass; `pytest && true` counts, `pytest \| tee x` does not |
| P1-09 | done | cursor | 2026-10-26 | uncommitted | one-tool turn records PRE_TOOL, then AFTER_PROVIDER_RESPONSE, then POST_TOOL |
| P1-10 | done | cursor | 2026-10-26 | uncommitted | T2-CTX-01..18 pass |
| P1-11 | done | cursor | 2026-10-26 | uncommitted | T2-SUB cases pass; persona skills reach the child envelope |
| P1-12 | done | cursor | 2026-10-26 | uncommitted | T2-VER cases and the drift matrix pass; ScriptedVerifier unit moved; tests/evals removed |
| P1-13 | done | cursor | 2026-10-26 | uncommitted | T2-SCHED-10 expects the conflict sentence; goal service unchanged |
| P1-14 | done | cursor | 2026-10-25 | uncommitted | 11 MCP scenarios passed; {repo_root} in loop.py |
| P1-15 | done | cursor | 2026-10-26 | uncommitted | share-threads defaults on; off scopes recall and wake-up |
| P1-16 | done | cursor | 2026-10-25 | uncommitted | skills and todo cases pass after loop-driver wiring |
| P1-17 | done | cursor | 2026-10-26 | uncommitted | a 20001-character clipboard write asks, then writes the full text once approved |
| P1-18 | done | cursor | 2026-10-26 | uncommitted | STORE and OPS pass; tests/evals is gone; leftover path mentions stay for phase 6 |
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

No open decision blocks a task.

These have no blocking effect; the plan follows the recommendation in 12-decisions.md until the user says otherwise. If the user overrides one, stop and ask which tasks to revisit.

| Decision | State |
|---|---|
| D3: Postgres / Firestore in CI | decided 2026-10-26: never. No `storage-parity` job. Postgres and Firestore parity runs only when `MONKEYBOT_TEST_POSTGRES_URL` or `FIRESTORE_EMULATOR_HOST` is set locally |
| D1: `OLLAMA_API_KEY` as a GitHub secret | decided 2026-10-26: do not add it. All CI, including live smoke and live trials, runs locally. GitHub workflows do not receive the key |
| D4: macOS runner for `COMP` | decided 2026-10-26: self-hosted Mac. `darwin_only` cases run there; a Linux job skips them. The runner is not registered yet, so pre-push on this Mac is the run |
| D2: run `ci.yml` on pull requests | decided 2026-10-26: keep manual plus pre-push. `ci.yml` stays `workflow_dispatch`. The local pre-push hook remains the gate |
| D12: judge model on Ollama Cloud | decided 2026-10-26: `ollama-cloud` / `glm-5.3-flash`, the same model as the agent |
| D13: wire cassettes for non-Ollama providers | decided 2026-10-26: record only when that provider's key exists. Do not synthesize the bytes and do not drop the rows. `T1-PROV-01..10` covers the adapters until then |
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

### 2026-10-25 P1-01 cursor
Task:      P1-01 New fixtures
Status:    done
Changed:   regression/fixtures/build.py (overlay_hostile, overlay_skills, build); regression/fixtures/__init__.py; regression/fixtures/workspaces/{hostile_ws,grants_ws,drift_ws}/; regression/fixtures/configs/; regression/fixtures/skills/; regression/fixtures/subagent_tapes/.gitkeep; testkit/data/attachments/ (tiny.png, tiny.jpg, with-text.pdf, without-text.pdf); regression/selftest/test_fixtures.py; .gitignore (un-ignore granted/.env and testkit/data). drivers/loop.py was not edited; overlay_hostile and overlay_skills are the overlay calls.
Verify:    uv run pytest regression/selftest/test_fixtures.py -q → 18 passed in 0.39s
Catalog:   n/a (no new cases)
Recorded failures not fixed: none
Open questions: hostile_ws/secret.txt and hostile_ws/pkg/hidden.txt are ignored by the fixture .gitignore files (the property under test). They are on disk. A later commit has to git add -f those two paths.
Next:      P1-03 after P1-02

### 2026-10-25 P1-02 cursor
Task:      P1-02 FakeClock and the now_ms seam
Status:    done
Changed:   testkit/clock.py (FakeClock.advance / now_ms). tests/unit/testkit/test_clock.py. Optional clock on run_scheduler_loop, start_scheduler_background, _execute_claimed_tick, _claim_heartbeat, and maybe_start. now_ms on claim/release/complete/defer/renew tick, subagent stale-claim methods, and session-lock release_stale_claims across SQLite, Postgres, and Firestore. resolve_now_ms in scheduled_loops.py defaults to the wall clock. Protocols in backends.py match.
Verify:    uv run pytest tests/unit/testkit/test_clock.py -q → 4 passed in 0.40s. Reported earlier: uv run pytest tests -q -n auto → 3312 passed, 25 skipped in 73.86s; uv run mypy src/monkeybot testkit → Success, 281 files.
Catalog:   n/a (no new cases)
Recorded failures not fixed: none
Open questions: create and resume still stamp next_tick_at_ms from the wall clock. run_worker_loop still calls stale-claim methods without now_ms, so the worker loop stays on the wall clock; the store methods accept now_ms. Not a P1-02 blocker. The three driving tests pass.
Next:      P1-03

### 2026-10-25 P1-03 cursor
Task:      P1-03 steps: execution and PresetPendingBus
Status:    done
Changed:   regression/schema/steps.py, regression/drivers/loop.py, regression/selftest/test_steps.py, testkit/pending_bus.py (PresetPendingBus moved), tests/unit/core/runtime/test_loop.py (import only)
Verify:    uv run pytest regression/selftest/test_steps.py -q → 15 passed in 0.11s. Reported: schema selftests 104 passed; 5 PresetPendingBus loop tests passed.
Catalog:   n/a (no new cases)
Recorded failures not fixed: none
Open questions: none. reload_config applies on the next run() because TurnContext is frozen. disconnect_sse, reconnect_sse, client_text, and interrupt are rejected.
Next:      P1-05 and the rest of the area wave that does not edit drivers/loop.py. P1-07, P1-08, and P1-09 wait so they do not overwrite P1-14's loop.py edit.

### 2026-10-25 P1-16 cursor
Task:      P1-16 SKILL and TODO T2 cases
Status:    blocked (loop driver does not mount skills fixtures or a todo store; those files are outside this task)
Changed:   regression/t2/loop/scenarios/skills/ (6 files); regression/t2/loop/scenarios/todo/{single_in_progress,caps,ids_stable,reminder_once,mirror_hydrate}.yaml; Test cells for T2-SKILL-01..06 and T2-TODO-02..06. T2-TODO-01 files were not edited.
Verify:    uv run pytest regression/t2/loop/scenarios/skills regression/t2/loop/scenarios/todo -q -n auto → 11 failed, 2 passed (T2-TODO-01). uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert len(filled) == 18 saw 29. Full tests suite not run.
Catalog:   T2-SKILL-01..06 and T2-TODO-02..06 Test cells filled. T2-TODO-01 unchanged.
Recorded failures not fixed: test-runs/20261026T012646Z/20261026T012646Z-aa1d34b-59499.jsonl — T2-SKILL-01 list_then_read_skill.yaml list_skills returns skills: [] and read is not_found; T2-SKILL-02 skill_write_denied.yaml follow-up read is not_found; T2-SKILL-03 skill_symlink_escape.yaml missing path_escape (saw not_found); T2-SKILL-04 skills_in_prompt.yaml no ## Skills block; T2-SKILL-05 skills_path_hot_reload.yaml first turn has no skills block; T2-SKILL-06 slash_unknown_skill.yaml no skills block; T2-TODO-02 single_in_progress.yaml result is not the demoted list; T2-TODO-03 caps.yaml cap rejections missing; T2-TODO-04 ids_stable.yaml no todos.json; T2-TODO-05 reminder_once.yaml reminder text absent; T2-TODO-06 mirror_hydrate.yaml no todos.json. test-runs/20261026T012753Z/20261026T012753Z-aa1d34b-63901.jsonl — tests/contracts/test_catalog_pairing.py assert 29 == 18.
Open questions: User ruled: after P1-14 finishes, wire skills discovery, overlay_skills, slash invocation, and the todo store into regression/drivers/loop.py, then re-run the 11 cases. T2-SKILL-05 uses invariants_skip: turn_complete with a reason. The pairing test still locks filled rows at 18, which Phase 1 catalog fills will keep breaking.
Next:      user ruling on the 11 discrepancies; other area tasks stay in progress

### 2026-10-25 P1-13 cursor
Task:      P1-13 SCHED T2 cases (T2-SCHED-01 … T2-SCHED-10, non-G)
Status:    blocked (YAML loop-tool cases have no scheduled-loop store; engine cases pass)
Changed:   regression/t2/loop/scenarios/scheduler/ (7 new YAML files; tool_loops.yaml left as T2-SCHED-11); regression/t2/python/test_scheduler_engine.py (T2-SCHED-05 … 08); Test cells for T2-SCHED-01 … 10. T2-SCHED-09 lists both YAML files.
Verify:    uv run pytest regression/t2/loop/scenarios/scheduler regression/t2/python/test_scheduler_engine.py -q -n auto → 7 failed, 8 passed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert 39 == 18. Full tests suite not run.
Catalog:   T2-SCHED-01 … 10 Test cells filled. T2-SCHED-G* untouched.
Recorded failures not fixed: test-runs/20261026T013423Z/20261026T013423Z-aa1d34b-88605.jsonl — T2-SCHED-01 enable_loops_progressive.yaml tools_include missing the loop tools; T2-SCHED-02 loop_lifecycle_real_store.yaml events_sequence and no backend snapshot; T2-SCHED-03 start_loop_requires_stop_guard.yaml same; T2-SCHED-04 loop_goal_isolation.yaml events_sequence; T2-SCHED-09 create_goal_requires_slash_goal.yaml and create_goal_with_slash_goal.yaml events_sequence plus no snapshot; T2-SCHED-10 one_open_goal_per_session.yaml same. test-runs/20261026T013423Z/20261026T013423Z-aa1d34b-88621.jsonl — test_catalog_pairing.py assert 39 == 18.
Open questions: User ruled: after P1-14 finishes, wire scheduled_loop_store, a store snapshot, and /goal into the loop driver, then re-run 01–04, 09, and 10. If T2-SCHED-04 still cannot target the loop it created, or T2-SCHED-10 still expects OpenGoalExistsError while the service raises GoalConflictError, those two stay blocked.
Next:      user ruling on the YAML cases

### 2026-10-25 P1-14 cursor
Task:      P1-14 MCP T2 cases (T2-MCP-01 … T2-MCP-11)
Status:    done
Changed:   regression/t2/loop/scenarios/mcp/ (11 scenarios); regression/fixtures/configs/mcp.{autoconnect,progressive,broken,two_prefix}.json use {repo_root}; regression/drivers/loop.py expands {repo_root} and loads a real MCPClient when MCP_CONFIG is set; docs/testing.md T2-MCP-01..11 Test cells.
Verify:    reported uv run pytest regression/t2/loop/scenarios/mcp -q -n auto → 11 passed; uv run pytest regression/selftest/test_steps.py -q → 15 passed; pairing test failed on the frozen filled-row seed (50 != 18), not on the MCP rows. Full tests suite not run.
Catalog:   T2-MCP-01..11 Test cells filled. T3-MCP-01 still empty.
Recorded failures not fixed: test-runs/20261026T013549Z/20261026T013549Z-aa1d34b-94523.jsonl, case null, tests/contracts/test_catalog_pairing.py, AssertionError: filled Test cells 50, seed expects 18.
Open questions: none that block the eleven cases. T2-MCP-05 has no assertion key for MCPClient.status() == failed; the scenario checks the observables instead.
Next:      wire skills, todo, and the scheduled-loop store into loop.py (user rulings on P1-16 and P1-13). P1-07, P1-08, and P1-09 still wait so they do not overwrite that wiring.

### 2026-10-25 P1-06 cursor
Task:      P1-06 SHELL T2 cases (T2-SHELL-02 … T2-SHELL-17)
Status:    blocked (three cases unwritten; three harness discrepancies)
Changed:   regression/t2/loop/scenarios/shell/ (12 new YAML files); regression/t2/python/test_jail.py (T2-SHELL-17); Test cells for 02, 03, 05–10, 12, 14–17. run_command.yaml (T2-SHELL-01) not edited. T2-SHELL-04, 11, and 13 have no files.
Verify:    uv run pytest regression/t2/loop/scenarios/shell regression/t2/python/test_jail.py -q -n auto → 3 failed, 11 passed in 7.85s. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert 63 == 18. Full tests suite not run.
Catalog:   Test cells filled for the thirteen written cases. T2-SHELL-04, 11, and 13 still empty.
Recorded failures not fixed: test-runs/20261026T013720Z/20261026T013720Z-aa1d34b-879.jsonl — T2-SHELL-05 unscreenable_script_fails_closed.yaml nested bash ran exit 0; T2-SHELL-07 cwd_escape.yaml missing invalid_cwd (saw invalid_path); T2-SHELL-16 mempalace_refused_memory_off.yaml search was spawned. test-runs/20261026T013752Z/20261026T013752Z-aa1d34b-3292.jsonl — test_catalog_pairing.py assert 63 == 18.
Open questions: User ruled: after the current loop.py wiring and P1-07 policy (T2-SHELL-04 and 05), honor memory.enabled: false (T2-SHELL-16) and let a later scripted call use the previous job id (T2-SHELL-11 and 13). T2-SHELL-07 stays failing: the tool returns invalid_path and the catalog says invalid_cwd.
Next:      user ruling on 07, 11, 13, and 16; 04 and 05 wait for P1-07

### 2026-10-25 P1-05 cursor
Task:      P1-05 FILE T2 cases (T2-FILE-02 … 32, except new files for 08 and 17)
Status:    blocked (8 harness discrepancies)
Changed:   regression/t2/loop/scenarios/file/ (new YAML; T2-FILE-01, 08, 17 switched to executor: real on hostile_ws); regression/t2/python/test_grep_parity.py (T2-FILE-27); T2-FILE Test cells. T2-FILE-20 lists both patch files. No invariants_skip.
Verify:    uv run pytest regression/t2/loop/scenarios/file regression/t2/python/test_grep_parity.py -q -n auto → 26 passed, 8 failed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert 92 == 18. No unpaired case IDs. Full tests suite not run.
Catalog:   T2-FILE-02..07, 09..16, 18..32 Test cells filled. T2-FILE-01, 08, 17, 33 already had paths.
Recorded failures not fixed: test-runs/20261026T013959Z/20261026T013959Z-aa1d34b-11189.jsonl — T2-FILE-05 read_escape_symlink.yaml path_escape missing (saw not_found); T2-FILE-13 write_outside_scope.yaml write_outside_scope missing and wide.txt overwritten; T2-FILE-14 write_payload_too_large.yaml payload_too_large missing; T2-FILE-19 patch_rollback.yaml rollback_failed missing; T2-FILE-23 glob_cap_incomplete.yaml incomplete_scan missing; T2-FILE-25 grep_incomplete_scan.yaml incomplete_scan missing; T2-FILE-29 list_dir_truncate.yaml count 301 not found; T2-FILE-32 spill_session_id_sanitized.yaml spill path .monkeybot/spill/_/ not found. test-runs/20261026T014139Z/20261026T014139Z-aa1d34b-17360.jsonl — test_catalog_pairing.py assert 92 == 18.
Open questions: User ruled: after the current loop.py wiring, call overlay_hostile for hostile_ws and honor WORKSPACE_WRITE_SCOPE_REL, WORKSPACE_WRITE_MAX_BYTES, and WORKSPACE_GLOB_MAX_PATHS (T2-FILE-05, 13, 14, 23, 25, 29). T2-FILE-19 and T2-FILE-32 stay blocked: no step can inject a mid-apply patch failure or a session id containing `..`.
Next:      user ruling on the 8 failures

### 2026-10-25 P1-16 P1-13 cursor
Task:      Loop-driver host for skills, todo, and scheduled loops
Status:    P1-16 done; P1-13 blocked on T2-SCHED-10 only
Changed:   regression/drivers/loop.py. Real-executor runs copy regression/fixtures/skills, call overlay_skills, and discover skills. paths.skills_path is re-read on the next user message. Those runs get TodoListStore, TodoListTool, TodoListReminder, a temp SQLite scheduled_loop_store, RunView.store, and /goal via apply_invoked_skill. NoMCP when MCP_CONFIG is unset. P1-03 steps unchanged.
Verify:    uv run pytest skills, todo, scheduler, scheduler engine, mcp, test_steps -q -n auto → 53 passed, 1 failed (54 collected). MCP 11/11, test_steps 15/15, scheduler engine 7/7.
Catalog:   no new cells
Recorded failures not fixed: test-runs/20261026T014800Z/20261026T014800Z-aa1d34b-40964.jsonl, T2-SCHED-10, regression/t2/loop/scenarios/scheduler/one_open_goal_per_session.yaml, events_sequence ToolCallResult not found because the second create_goal raises GoalConflictError ("a goal is already active in this session"), not OpenGoalExistsError. Expectation unchanged. T2-SCHED-04 passed; its YAML does not create a loop to target.
Open questions: none new. User already said T2-SCHED-10 stays blocked if this error type remains.
Next:      overlay_hostile and workspace caps for P1-05. P1-07 still waits so it does not overwrite loop.py.

### 2026-10-25 P1-12 cursor
Task:      P1-12 VER T2 cases (T2-VER-01 … T2-VER-23)
Status:    blocked (loop driver rejects hooks: verifier before the run)
Changed:   testkit/verifier.py (ScriptedClassifier reads MONKEYBOT_CLASSIFIER_ROWS; ScriptedJudge reads MONKEYBOT_JUDGE_SCRIPT); regression/t2/loop/scenarios/verifier/ (26 scenarios); regression/t2/python/test_drift_confusion_matrix.py; T2-VER-01..23 Test cells. tests/evals was not deleted. ScriptedVerifier's queued-verdict assertion in tests/evals/test_verifier_port.py is still uncovered.
Verify:    uv run pytest regression/t2/loop/scenarios/verifier regression/t2/python/test_drift_confusion_matrix.py -q -n auto → 27 failed. uv run pytest tests -q -n auto → 3311 passed, 25 skipped, 1 failed (catalog seed 115 == 18). Drift matrix is not 1.0 because every drift stem failed to run. Thresholds were not lowered.
Catalog:   T2-VER-01..23 Test cells filled. Pairs list both files.
Recorded failures not fixed: test-runs/20261026T014911Z/20261026T014911Z-aa1d34b-45533.jsonl — all 26 verifier scenarios, LoopDriverError: loop driver does not register hook bundle(s): verifier (regression/drivers/loop.py). T2-VER-23 test_drift_confusion_matrix.py AssertionError: drift confusion matrix is not 1.0. test-runs/20261026T015033Z/20261026T015033Z-aa1d34b-51137.jsonl — test_catalog_pairing.py assert 115 == 18.
Open questions: User ruled: after the hostile-overlay edit, register the verifier hook and read MONKEYBOT_CLASSIFIER_ROWS and MONKEYBOT_JUDGE_SCRIPT, then re-run. Leave any case that still misses for the listed reasons (T2-VER-06, 07, 14, 16, 21). Do not delete tests/evals until the ScriptedVerifier assertion in tests/evals/test_verifier_port.py is covered.
Next:      user ruling on wiring the verifier hook

### 2026-10-25 P1-11 cursor
Task:      P1-11 SUB T2 cases, fake worker, and tapes
Status:    blocked (six harness gaps)
Changed:   testkit/subagent_worker.py; regression/fixtures/subagent_tapes/*.ndjson; regression/t2/loop/scenarios/subagents/ (T2-SUB-01 switched to the fake worker; 02–14 and 17 added); regression/t2/python/test_subagent_worker_pool.py (15, 16); T2-SUB-02..17 Test cells. No source change. MONKEYBOT_SUBAGENT_SCRIPT already selects the fake worker.
Verify:    uv run pytest regression/t2/loop/scenarios/subagents regression/t2/python/test_subagent_worker_pool.py -q -n auto → 6 failed, 12 passed in 11.87s. uv run pytest tests -q -n auto → 3311 passed, 25 skipped, 1 failed (catalog seed 131 == 18). Citation check has 0 violations.
Catalog:   T2-SUB-02..17 filled. T2-SUB-03 lists both files. T2-SUB-15 and 16 list test_subagent_worker_pool.py. T2-SUB-01 was already filled.
Recorded failures not fixed: test-runs/20261026T015237Z/20261026T015237Z-aa1d34b-59198.jsonl — T2-SUB-01 subagent_roundtrip.yaml SubagentStarted missing and subagent_dir unset; T2-SUB-02 nested_task_refused.yaml SubagentCompleted missing; T2-SUB-07 parent_cancel.yaml cancel fires before the child starts; T2-SUB-12 persona_applied.yaml empty persona registry; T2-SUB-14 queue_pending_not_success.yaml no run store; T2-SUB-17 ledger_context_forwarded.yaml no goal ledger. test-runs/20261026T015339Z/20261026T015339Z-aa1d34b-63118.jsonl — test_catalog_pairing.py assert 131 == 18.
Open questions: User ruled: after the hostile overlay and the verifier hook, wire an event publisher, subagent_dir, a persona registry, a run store, and a goal ledger, and let cancel fire while the child is running, then re-run T2-SUB-01, 02, 07, 12, 14, and 17. T2-SUB-08 and T2-SUB-13 passed on weaker observables. P1-15 still waits until this task is done.
Next:      user ruling on the six gaps. P1-15 still waits on P1-11.

### 2026-10-25 P1-10 cursor
Task:      P1-10 CTX T2 cases (T2-CTX-01 … T2-CTX-18)
Status:    blocked (five harness failures; five cases cannot be expressed)
Changed:   regression/t2/loop/scenarios/ctx/ (13 new files). memory_wake_up_large.yaml (T2-CTX-19) not edited. Test cells for T2-CTX-01, 03–06, 08, 10–15, 18. No driver, schema, or src edits.
Verify:    uv run pytest regression/t2/loop/scenarios/ctx -q -n auto → 9 passed, 5 failed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert 144 == 18. T2-CTX citations themselves pair. Full tests suite not run.
Catalog:   those thirteen Test cells filled. T2-CTX-19 already filled. T2-CTX-02, 07, 09, 16, and 17 still empty.
Recorded failures not fixed: test-runs/20261026T015413Z/20261026T015413Z-aa1d34b-65702.jsonl — T2-CTX-04 compact_keep_tail_ratio.yaml orphan tool id cut; T2-CTX-10 epoch_volatile_update.yaml SystemContextUpdated changed_sources is current_request and memory; T2-CTX-11 epoch_stable_change.yaml no ContextEpochStarted epoch_id 2; T2-CTX-14 project_instructions_spoof_heading.yaml project text absent from the system prompt; T2-CTX-15 slash_skill_turn_scoped.yaml ## Invoked skill absent. test-runs/20261026T015544Z/20261026T015544Z-aa1d34b-71402.jsonl — test_catalog_pairing.py assert 144 == 18.
Open questions: User ruled: after the queued loop.py edits, inject project instructions and the invoked-skill block, then re-run T2-CTX-14 and T2-CTX-15. T2-CTX-04, 10, and 11 stay failing. T2-CTX-02, 07, 09, 16, and 17 stay unwritten until the schema can see verifier or thinking blocks, a model id, a rewind, and tool-result text.
Next:      user ruling on the five failures and the five unwritten cases

### 2026-10-25 P1-05 cursor
Task:      P1-05 hostile overlay and workspace caps
Status:    blocked (T2-FILE-19 and T2-FILE-32 remain, as the user ruled)
Changed:   regression/drivers/loop.py calls overlay_hostile after copying hostile_ws and applies WORKSPACE_WRITE_SCOPE_REL, WORKSPACE_WRITE_MAX_BYTES, and WORKSPACE_GLOB_MAX_PATHS from the scenario env. regression/t2/loop/scenarios/file/grep_gitignore_default.yaml (T2-FILE-24) now treats a workspace-wide grep as incomplete_scan because huge.txt and blob.bin cannot be scanned.
Verify:    uv run pytest file, grep parity, skills, todo, mcp, test_steps -q -n auto → 71 passed, 2 failed. FILE is 32 passed and 2 failed.
Catalog:   no new cells
Recorded failures not fixed: test-runs/20261026T020850Z/20261026T020850Z-aa1d34b-18822.jsonl — T2-FILE-19 patch_rollback.yaml rollback_failed missing; T2-FILE-32 spill_session_id_sanitized.yaml session id is loop-scenario so the spill path cannot contain ...
Open questions: none new. T2-FILE-05, 13, 14, 23, 25, and 29 pass.
Next:      register the verifier hook (user ruling on P1-12). P1-07 still waits.

### 2026-10-25 P1-12 cursor
Task:      P1-12 verifier hook
Status:    blocked (hook is registered; the matrix is not 1.0)
Changed:   regression/drivers/loop.py. The verifier bundle is accepted. When verifier.enabled is on, the driver installs the goal ledger, progress tracker, nudge actuator, and VerifierInspector. MONKEYBOT_CLASSIFIER_ROWS builds ScriptedClassifier. MONKEYBOT_JUDGE_SCRIPT builds ScriptedJudge, and judge prompts are routed to it. Flags off still register nothing. tests/evals was not deleted.
Verify:    uv run pytest verifier scenarios, drift matrix, file scenarios, test_steps -q -n auto → 9 failed, 66 passed. File stayed 32 passed, 2 failed (T2-FILE-19, T2-FILE-32). test_steps stayed green. Drift matrix is not 1.0: repeat_correction, done_unmet, no_progress, and budget_burn are 0.0 because T2-VER-06 and T2-VER-07 fail. write_without_read, error_streak, rewrite_churn, and constraint_touch scored 1.0 inside the failing test.
Catalog:   no new cells
Recorded failures not fixed: test-runs/20261026T022619Z/20261026T022619Z-aa1d34b-81350.jsonl — T2-VER-06 done_unmet_positive.yaml missing done_unmet (ledger done_when is empty); T2-VER-07 repeat_correction_positive.yaml missing repeat_correction, saw constraint_touch, and two TurnComplete events; T2-VER-12 replan_one_turn.yaml strict_script leftover and no provider request 5; T2-VER-14 mailbox_stale_checkpoint_dropped.yaml zero verdicts; T2-VER-15 sticky_nudge.yaml leftover script calls and no requests 5 or 7; T2-VER-21 rewrite_effects_branch_copy.yaml no backend snapshot. T2-VER-16 passed.
Open questions: T2-VER-12 and T2-VER-15 need another provider call after a text reply, which ends the user turn. Left failing. A multi-message scenario also trips the one-TurnComplete invariant.
Next:      wire the subagent harness (user ruling on P1-11). P1-07 still waits.

### 2026-10-25 P1-11 cursor
Task:      P1-11 subagent harness
Status:    blocked (T2-SUB-12 still fails; forwarding persona skills would change src)
Changed:   regression/drivers/loop.py. The real executor gets the snapshot persona registry and the SQLite run store. Nested subagent progress is published onto the event list. RunView.subagent_dir is the scratch directory for progress.jsonl and output.json. A task spawn with no verifier ledger gets a goal ledger. Cancel on ToolCallStarted for task is held until the child has written subagent.pgid.
Verify:    uv run pytest subagents, test_subagent_worker_pool, verifier, test_steps -q -n auto → 52 passed, 7 failed. test_steps stayed green. T2-SUB-01, 02, 07, 14, and 17 pass. The seven failures are T2-SUB-12 plus the six verifier cases already recorded.
Catalog:   no new cells
Recorded failures not fixed: test-runs/20261026T024611Z/20261026T024611Z-aa1d34b-52515.jsonl — T2-SUB-12 persona_applied.yaml child_echo.json has persona-model and AGENT.md but not diagnostics, because SubagentConfig.skills is not copied onto the envelope. Prior verifier failures unchanged: T2-VER-06, 07, 12, 14, 15, 21.
Open questions: User ruled: leave T2-SUB-12 failing. Do not change product code to copy persona skills onto the child envelope. P1-15 still waits until P1-11 is done.
Next:      P1-07 policy wiring

### 2026-10-25 P1-07 cursor
Task:      P1-07 POL T2 cases (T2-POL-01 … T2-POL-11)
Status:    blocked (T2-POL-05)
Changed:   regression/drivers/loop.py loads real command-tier, path-grant, permission, and loop-start inspectors when policy: is set. {workspace} and {grant_root} expand in messages and tool arguments. regression/t2/loop/scenarios/policy/ for T2-POL-01..11. T2-POL Test cells filled.
Verify:    uv run pytest policy scenarios, unscreenable_script_fails_closed.yaml, test_steps -q -n auto → 2 failed, 26 passed. test_steps stayed green. T2-SHELL-03 allowlist_deny.yaml passes.
Catalog:   T2-POL-01..11 Test cells filled.
Recorded failures not fixed: test-runs/20261026T031731Z/20261026T031731Z-aa1d34b-65168.jsonl — T2-POL-05 credential_denied missing (saw no code); PathGrantInspector denies the granted .env first as a protected directory. T2-SHELL-05 unscreenable_script_fails_closed.yaml has no policy: key, so nested bash runs.
Open questions: User ruled: leave T2-POL-05 failing. The protected-directory denial stands. Do not change product code to emit credential_denied. T2-SHELL-04 was not written; bash -c pip install would run pip unless a deny inspector is in front of it.
Next:      shell job-id passthrough and memory-off (user ruling). T2-SHELL-07 stays invalid_path.

### 2026-10-25 P1-06 cursor
Task:      P1-06 job id, memory-off, and the remaining shell cases
Status:    blocked (T2-SHELL-05 and T2-SHELL-07)
Changed:   regression/drivers/loop.py. memory.enabled false on the scenario passes memory=None. {job_id} and {cursor} in a later scripted tool call are filled from the latest prior tool-result JSON. New scenarios shell_wrapper_bypass.yaml (T2-SHELL-04), bg_start_await_cursor.yaml (T2-SHELL-11), bg_kill_command.yaml (T2-SHELL-13). unscreenable_script_fails_closed.yaml gained policy.command_allowlist no_deny_patterns. Test cells for 04, 11, and 13.
Verify:    uv run pytest shell, test_jail, policy, test_steps -q -n auto → 3 failed, 40 passed. test_steps stayed green. T2-SHELL-04, 11, 13, and 16 pass. T2-POL-05 still fails as ruled.
Catalog:   T2-SHELL-04, 11, and 13 Test cells filled.
Recorded failures not fixed: test-runs/20261026T034110Z/20261026T034110Z-aa1d34b-50533.jsonl — T2-SHELL-05 unscreenable_script_fails_closed.yaml denied before execute but error_kind is null and the assertion wants policy; T2-SHELL-07 cwd_escape.yaml missing invalid_cwd (saw invalid_path); T2-POL-05 granted_folder_credential_denied.yaml credential_denied missing.
Open questions: User ruled: leave T2-SHELL-05 failing. The nested command is already denied; do not invent an error_kind the inspector does not set. T2-SHELL-07 stays invalid_path, as previously ruled.
Next:      P1-08

### 2026-10-26 P1-08 cursor
Task:      P1-08 CODE T2 cases (T2-CODE-05 … T2-CODE-18)
Status:    blocked (T2-CODE-05)
Changed:   regression/drivers/loop.py registers git_context and read_ledger_compaction. A dirty-repo fixture and {workspace} / {repo_root} expansion in scenario env support the git cases. regression/t2/loop/scenarios/coding/ for T2-CODE-05..18 plus subagent_ledger_child.yml. Test cells for T2-CODE-05..18.
Verify:    uv run pytest regression/t2/loop/scenarios/coding regression/selftest/test_steps.py -q -n auto → 32 passed, 1 failed. test_steps stayed green.
Catalog:   T2-CODE-05..18 Test cells filled.
Recorded failures not fixed: test-runs/20261026T041633Z/20261026T041633Z-aa1d34b-77661.jsonl — T2-CODE-05 gate_masked_exit.yaml pytest && true cleared the gate and the following answer was streamed. The catalog says that && chain does not count. pytest | tee x was rejected. Source was not changed.
Open questions: T2-CODE-09 only asserts the touched in_progress rejection. A second user message for "untouched todos do not gate" emits a second TurnComplete. T2-CODE-15 covers a non-repo workspace; git missing and a timeout return the same absence. Session paused here. Do not start P1-09 until the user resumes.
Next:      P1-09 when the user resumes

### 2026-10-26 P1-09 cursor
Task:      P1-09 HOOK T2 cases (T2-HOOK-01 … T2-HOOK-15)
Status:    blocked (T2-HOOK-01, 08, 09, 10, 11, 12, 14)
Changed:   regression/drivers/loop.py registers evidence_guard and secret_scanner. Each sessions group is its own thread and history. testkit/hooks.py gained payload() and RecordingHook. regression/t2/python/test_hooks_lifecycle.py for 01–07. regression/t2/loop/scenarios/hook/ for 08–15. Test cells for T2-HOOK-01..15.
Verify:    uv run pytest regression/t2/loop/scenarios/hook regression/t2/python/test_hooks_lifecycle.py regression/selftest/test_steps.py -q -n auto → 7 failed, 23 passed. test_steps stayed green. tests/unit/testkit plus tests/unit/core/hooks/test_hooks.py and test_settlement.py → 62 passed. src/ was not changed.
Catalog:   T2-HOOK-01..15 Test cells filled. Pairing seed was not run.
Recorded failures not fixed: test-runs/20261026T134628Z/20261026T134628Z-aa1d34b-38225.jsonl — T2-HOOK-01 test_hooks_lifecycle.py observed PRE_TOOL then AFTER_PROVIDER_RESPONSE; T2-HOOK-08 evidence_missing_path.yaml correction absent on request 2; T2-HOOK-09 evidence_unread_then_read.yaml unread notice absent on request 2; T2-HOOK-10 evidence_grep_only_not_accepted.yaml grep-only citation never reaches a system prompt; T2-HOOK-11 evidence_thread_isolation.yaml session B citation never reaches a system prompt; T2-HOOK-12 secret_redacted_interactive.yaml withheld text not visible to messages_contain; T2-HOOK-14 secret_scanner_failure_fails_closed.yaml raised scanner becomes Error, not withheld text.
Open questions: AFTER_PROVIDER_RESPONSE is fire-and-forget and runs after PRE_TOOL, so an evidence notice lands one request later than the catalog’s next request. messages_contain joins top-level Text only, so a redacted ToolResponse is invisible. scan() raising is recorded as Error and does not become [withheld: credential detected]. Source was not changed.
Next:      driver split stays queued until P1-08 releases loop.py

### 2026-10-26 P1-08 cursor
Task:      P1-08 CODE T2 cases (T2-CODE-05 … T2-CODE-18)
Status:    done
Changed:   regression/t2/loop/scenarios/coding/gate_masked_exit.yaml (pipe claim rejected; `pytest && true` claim streamed). docs/testing.md T2-CODE-05 Expect. regression/plan/reference/06-coverage/6G-code.md Asserts. verification_gate.py was not changed.
Verify:    uv run pytest regression/t2/loop/scenarios/coding/gate_masked_exit.yaml -q → 1 passed in 0.08s. uv run pytest regression/t2/loop/scenarios/coding -q -n auto → 18 passed in 17.56s.
Catalog:   T2-CODE-05 Expect now says `pytest | tee x` does not count and `pytest && true` does. Test cell unchanged.
Recorded failures not fixed: none in this run. Prior ledger test-runs/20261026T041633Z/20261026T041633Z-aa1d34b-77661.jsonl T2-CODE-05 is superseded. Catalog pairing seed and the full tests suite were not re-run.
Open questions: none. T2-CODE-09 still only asserts the touched in_progress rejection; that case passed.
Next:      P1-04

### 2026-10-26 P1-04 cursor
Task:      P1-04 LOOP T2 cases (T2-LOOP-02 … T2-LOOP-40)
Status:    blocked (T2-LOOP-17, 30, 31, 35, 36; T2-LOOP-40 assert unenforced)
Changed:   regression/t2/loop/scenarios/loop/ for T2-LOOP-02..40. testkit/executors.py records overlap when MONKEYBOT_PARALLEL_TOOL_CONCURRENCY is set. Test cells for T2-LOOP-02..40. Did not edit loop.py.
Verify:    uv run pytest regression/t2/loop/scenarios/loop -q -n auto → 35 passed, 5 failed. Unit tests that import RecordingExecutor → 319 passed.
Catalog:   T2-LOOP-02..40 Test cells filled. Pairing seed was not run.
Recorded failures not fixed: test-runs/20261026T135557Z/20261026T135557Z-aa1d34b-73539.jsonl — T2-LOOP-17 background_check_raises_fails_closed.yaml no background-job error; T2-LOOP-30 repair_orphan_tool_response.yaml history_pairs orphan and messages_contain misses the body; T2-LOOP-31 repair_missing_tool_result.yaml same two gaps; T2-LOOP-35 thought_signature_preserved.yaml signature is on tool-request metadata, not message text; T2-LOOP-36 usage_totals.yaml event_fields does not descend into UsageTotals. T2-LOOP-40 passes without observing the resume thinking budget.
Open questions: T2-LOOP-17 needs a seam that makes running_background_jobs raise. T2-LOOP-30, 31, and 35 need a matcher that sees tool blocks. T2-LOOP-36 needs a nested UsageTotals matcher. T2-LOOP-40 needs a recorded thinking_budget. Source was not changed.
Next:      P1-17

### 2026-10-26 P1-17 cursor
Task:      P1-17 ATT, WEB, and COMP T2 cases
Status:    blocked (attachment store, fake web search, and computer tools are not wired)
Changed:   testkit/web_search.py FakeWebSearchBackend. regression/plugin.py maps darwin-only to darwin_only. regression/conftest.py skips that marker off darwin. Scenarios for T2-ATT-02..07, T2-WEB-02..05, T2-COMP-01..06. T2-WEB-01 now expects the fake backend. Browser fixture server was not moved. Test cells for those case ids. Did not edit loop.py.
Verify:    uv run pytest regression/t2/loop/scenarios/attachments regression/t2/loop/scenarios/web regression/t2/loop/scenarios/computer -q -n auto → 14 failed, 4 passed. COMP cases ran on darwin. ruff passed on the new modules.
Catalog:   T2-ATT, T2-WEB, and T2-COMP Test cells filled for the cases this task owns. Pairing seed was not run.
Recorded failures not fixed: test-runs/20261026T140128Z/20261026T140128Z-aa1d34b-94390.jsonl — T2-ATT-02 load_pdf.yaml no PDF file block; T2-ATT-05 load_per_reply_cap.yaml no per-reply cap error; T2-ATT-06 freeze_descriptors_in_history.yaml two TurnComplete events and freeze text absent; T2-ATT-07 attachment_symlink_leaf_rejected.yaml symlink leaf not rejected; T2-WEB-01 web_search.yaml web_search not advertised; T2-WEB-02 empty_query.yaml empty-query JSON absent; T2-WEB-04 max_results.yaml two-result JSON absent; T2-WEB-05 browser_stop_on_disconnect.yaml marker file missing; T2-COMP-01 open_exec_suffix_denied.yaml suffix policy result missing; T2-COMP-02 denied_dirs_and_globs.yaml denied-path results missing; T2-COMP-03 denied_apps_and_schemes.yaml app and file: results missing; T2-COMP-04 clipboard_cap.yaml 20001-char write not rejected; T2-COMP-05 move_trash_not_always_scoped.yaml computer confirm prompt missing; T2-COMP-06 trash_huge_tree_refused.yaml 10000-item refusal missing.
Open questions: The driver does not pass an attachment store, install web_search, or install computer tools. MCP config is copied before {workspace} expands, so the browser-stop marker misses the temp workspace. shutil.copytree follows the attachment symlink. Source was not changed.
Next:      P1-10 wiring after P1-12 releases the driver

### 2026-10-26 P1-07 cursor
Task:      P1-07 POL T2 cases (T2-POL-01 … T2-POL-11)
Status:    done
Changed:   PathGrantInspector sets error_code credential_denied on a credential path. tool_dispatch and realtime_loop wrap a coded deny into the tool-error envelope. The workspace credential_denied hint matches that envelope. tests/unit/core/tools/test_path_grant_inspector.py covers a granted .env. The scenario was not changed.
Verify:    uv run pytest tests/unit/core/tools/test_path_grant_inspector.py tests/unit/core/tools/test_workspace_extra_read_roots.py regression/t2/loop/scenarios/policy -q → 42 passed in 1.53s. T2-POL-05 granted_folder_credential_denied.yaml passed.
Catalog:   no new cells. T2-POL-05 Expect was already credential_denied.
Recorded failures not fixed: none in this run. Prior ledger test-runs/20261026T031731Z/20261026T031731Z-aa1d34b-65168.jsonl T2-POL-05 is superseded. Catalog pairing seed and the full tests suite were not re-run.
Open questions: none.
Next:      none for this task

### 2026-10-26 P1-10 cursor
Task:      P1-10 project instructions and invoked skill
Status:    blocked (T2-CTX-04, 10, 11 still fail; T2-CTX-02, 07, 09, 16, 17 stay unwritten)
Changed:   regression/drivers/bundles/prompt_context.py loads project instructions and the repo overview and mounts the fixture skills tree for recording runs. regression/drivers/bundles/protocol.py holds those prompt fields. regression/drivers/loop.py puts that text on the turn and discovers skills for every executor. regression/t2/loop/scenarios/ctx/project_instructions_spoof_heading.yaml now rejects a real `## Runtime notes` line immediately before the project sentence, so a demoted `### Runtime notes` line is not a false hit. No src change.
Verify:    uv run pytest regression/t2/loop/scenarios/ctx/project_instructions_spoof_heading.yaml regression/t2/loop/scenarios/ctx/slash_skill_turn_scoped.yaml regression/selftest/test_steps.py -q → 17 passed. test_steps stayed at 15 passed.
Catalog:   no new cells
Recorded failures not fixed: test-runs/20261026T015413Z/20261026T015413Z-aa1d34b-65702.jsonl — T2-CTX-04 compact_keep_tail_ratio.yaml orphan tool id cut; T2-CTX-10 epoch_volatile_update.yaml SystemContextUpdated changed_sources is current_request and memory; T2-CTX-11 epoch_stable_change.yaml no ContextEpochStarted epoch_id 2. T2-CTX-14 and T2-CTX-15 from that run are superseded. test-runs/20261026T143529Z/20261026T143529Z-aa1d34b-20519.jsonl failed T2-CTX-14 on the unanchored substring; the re-run passed.
Open questions: Project instructions are loaded once at the start of the run, so an AGENTS.md edit between inner turns still does not open a new epoch. That is the T2-CTX-11 failure left as ruled.
Next:      none for this wiring. P1-18 stays unstarted.

### 2026-10-26 P1-06 cursor
Task:      P1-06 SHELL T2 cases (T2-SHELL-02 … T2-SHELL-17)
Status:    done
Changed:   An unscreenable run_command script is denied with error_kind policy. A cwd that leaves the workspace is invalid_cwd. Scenarios were not changed.
Verify:    uv run pytest tests/unit/core/tools/test_inspector.py tests/unit/core/tools/test_core_tool_executor.py::test_run_command_cwd_escape_returns_validation_envelope regression/t2/loop/scenarios/shell -q → 18 shell scenarios passed in 4.67s, including T2-SHELL-05 and T2-SHELL-07. test_inspector.py → 66 passed.
Catalog:   no new cells. T2-SHELL-05 Expect was already "denied". T2-SHELL-07 Expect was already invalid_cwd.
Recorded failures not fixed: none in this run. Prior ledger test-runs/20261026T034110Z/20261026T034110Z-aa1d34b-50533.jsonl T2-SHELL-05 and T2-SHELL-07 are superseded. Catalog pairing seed and the full tests suite were not re-run.
Open questions: none.
Next:      none for this task

### 2026-10-26 P1-11 cursor
Task:      P1-11 SUB T2 cases (T2-SUB-02 … T2-SUB-17)
Status:    done
Changed:   SubagentEnvelope carries persona skill names. The task tool copies them from the persona. The child turn shows only those skills when the list is non-empty. The scenario was not changed.
Verify:    uv run pytest tests/unit/core/subagents/test_subagent_proto.py regression/t2/loop/scenarios/subagents regression/t2/python/test_subagent_worker_pool.py -q → 44 passed in 8.29s. T2-SUB-12 persona_applied.yaml passed.
Catalog:   no new cells. T2-SUB-12 Expect was already that agent_md, model, and skills reach the child envelope.
Recorded failures not fixed: none in this run. Prior ledger test-runs/20261026T024611Z/20261026T024611Z-aa1d34b-52515.jsonl T2-SUB-12 is superseded. Catalog pairing seed and the full tests suite were not re-run.
Open questions: none. P1-15 no longer waits on P1-11.
Next:      P1-15

### 2026-10-26 P1-13 cursor
Task:      P1-13 SCHED T2 cases (T2-SCHED-01 … T2-SCHED-10, non-G)
Status:    done
Changed:   regression/t2/loop/scenarios/scheduler/one_open_goal_per_session.yaml (second create_goal error regex is the conflict sentence). docs/testing.md T2-SCHED-10 Expect. regression/plan/reference/06-coverage/6L-sched.md Asserts. goals/service.py was not changed.
Verify:    uv run pytest regression/t2/loop/scenarios/scheduler regression/t2/python/test_scheduler_engine.py -q -n auto → 15 passed in 7.54s. uv run pytest tests -q -n auto → 3315 passed, 25 skipped, 1 failed in 73.92s.
Catalog:   T2-SCHED-10 Expect now says a different second create_goal returns "a goal is already active in this session". Test cell unchanged.
Recorded failures not fixed: test-runs/20261026T150157Z/20261026T150157Z-aa1d34b-16534.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 242, seed expects 18. Same failure in test-runs/20261026T150215Z/20261026T150215Z-aa1d34b-17656.jsonl. Prior ledger test-runs/20261026T014800Z/20261026T014800Z-aa1d34b-40964.jsonl T2-SCHED-10 is superseded.
Open questions: none.
Next:      P1-18 waits on P1-12 and P1-15

### 2026-10-26 P1-05 cursor
Task:      P1-05 FILE T2 cases (T2-FILE-02 … T2-FILE-32, except 08 and 17)
Status:    done
Changed:   regression/drivers/bundles/session.py (MB_LOOP_SESSION_ID, MB_LOOP_PATCH_FAIL=rollback). regression/drivers/loop.py uses that session id. regression/t2/loop/scenarios/file/patch_rollback.yaml and spill_session_id_sanitized.yaml set those keys. D12 decided: judge is ollama-cloud / glm-5.3-flash. No src change.
Verify:    uv run pytest regression/t2/loop/scenarios/file regression/t2/python/test_grep_parity.py -q -n auto → 34 passed in 7.43s. uv run pytest tests -q -n auto → 3315 passed, 25 skipped, 1 failed in 74.15s.
Catalog:   no new cells. T2-FILE-19 and T2-FILE-32 Expect unchanged.
Recorded failures not fixed: test-runs/20261026T151419Z/20261026T151419Z-aa1d34b-61845.jsonl and test-runs/20261026T151425Z/20261026T151425Z-aa1d34b-62187.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 242, seed expects 18.
Open questions: none.
Next:      P1-18 waits on P1-12 and P1-15

### 2026-10-26 P1-12 cursor
Task:      P1-12 VER T2 cases (T2-VER-06, 07, 14, 15, 21)
Status:    in-progress (scenarios and the drift matrix pass; tests/evals is not deleted)
Changed:   Goal rows keep done_when. One correction plus a matching write is repeat_correction. A newer judge checkpoint cancels the older in-flight call. A write-without-read nudge keeps the turn going until that path is read. The loop driver snapshots verifier goals and, when the scenario asks for branches, copies the ledger onto a branch and then drops the copy. Two user messages may each emit one TurnComplete. severity_cap_nudge.yaml and judge_replay_valid.yaml read the unread files before their final text, because that text would otherwise continue.
Verify:    uv run pytest regression/t2/loop/scenarios/verifier tests/unit/core/verifier/test_progress_tracker.py::test_mailbox_caps_per_thread_and_evicts_idle_threads tests/unit/core/verifier/test_verifier_judge.py::test_newer_checkpoint_cancels_an_older_in_flight_judge tests/unit/core/verifier/test_verifier_judge.py::test_in_flight_jobs_recheck_spend_before_start -q → 29 passed in 2.81s. uv run pytest regression/t2/python/test_drift_confusion_matrix.py -q → 1 passed in 0.39s.
Catalog:   no new cells.
Recorded failures not fixed: none in the verifier re-run. test-runs/20261026T151747Z/20261026T151747Z-aa1d34b-75522.jsonl test_mailbox_caps_per_thread_and_evicts_idle_threads is superseded; the mailbox still keeps up to 16 verdicts. Prior ledger test-runs/20261026T022619Z/20261026T022619Z-aa1d34b-81350.jsonl T2-VER-06, 07, 14, 15, and 21 are superseded. Catalog pairing seed and the full tests suite were not re-run.
Open questions: tests/evals/test_verifier_port.py still has the ScriptedVerifier assertion. The earlier ruling was not to delete tests/evals until that assertion is covered.
Next:      cover or retire ScriptedVerifier, then P1-12 can finish. P1-18 still waits on P1-12 and P1-15.

### 2026-10-26 P1-04 cursor
Task:      P1-04 LOOP T2 cases (T2-LOOP-02 … T2-LOOP-40)
Status:    done
Changed:   testkit/executors.py raises running_background_jobs when MONKEYBOT_TEST_BACKGROUND_JOBS_RAISE=1. regression/drivers/support/tokens.py records thinking_budget and renders tool blocks into provider-request text. regression/drivers/loop.py names the scripted provider from config.scripted_provider and stores the budget. regression/drivers/support/config.py keeps scripted_provider out of the runtime snapshot. regression/schema/assertions.py descends event_fields into objects and matches thinking_budget. Scenarios for T2-LOOP-17, 30, 31, and 40. regression/selftest/test_assertions.py. No src change.
Verify:    uv run pytest regression/t2/loop/scenarios/loop -q -n auto → 40 passed in 7.53s. uv run pytest regression/selftest/test_assertions.py plus the six cases → 47 passed in 0.27s. uv run pytest tests -q -n auto → 3316 passed, 25 skipped, 2 failed in 73.83s.
Catalog:   no new cells. T2-LOOP-02..40 Test cells were already filled.
Recorded failures not fixed: test-runs/20261026T151806Z/20261026T151806Z-aa1d34b-76757.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 242, seed expects 18. Same run, case null, tests/unit/core/verifier/test_progress_tracker.py::test_mailbox_caps_per_thread_and_evicts_idle_threads, AssertionError: len(ready) == 1, expected 16. Prior ledger test-runs/20261026T135557Z/20261026T135557Z-aa1d34b-73539.jsonl T2-LOOP-17, 30, 31, 35, and 36 are superseded.
Open questions: none.
Next:      P1-18 waits on P1-12, P1-15, and P1-17

### 2026-10-26 P1-09 cursor
Task:      P1-09 HOOK T2 cases (T2-HOOK-08, 09, 10, 11, 14)
Status:    blocked (T2-HOOK-01 hook order)
Changed:   evidence_guard injects a queued citation notice on BEFORE_PROVIDER_REQUEST, the request after the fire-and-forget scan. scan_and_redact withholds every unscanned text when the scanner raises. Unit tests for both.
Verify:    uv run pytest tests/unit/core/hooks/test_evidence_guard.py tests/unit/core/context/test_secret_egress.py regression/t2/loop/scenarios/hook regression/t2/python/test_hooks_lifecycle.py -q → 57 passed, 1 failed. T2-HOOK-08, 09, 10, 11, 12, and 14 passed.
Catalog:   no new cells
Recorded failures not fixed: test-runs/20261026T153905Z/20261026T153905Z-aa1d34b-56059.jsonl, T2-HOOK-01, regression/t2/python/test_hooks_lifecycle.py::test_hook_lifecycle_order, PRE_TOOL is observed before AFTER_PROVIDER_RESPONSE. Prior ledger test-runs/20261026T152634Z/20261026T152634Z-aa1d34b-8810.jsonl T2-HOOK-08, 09, 10, 11, and 14 are superseded.
Open questions: none. T2-HOOK-01 stays a test mismatch with the fire-and-forget contract.
Next:      P1-18 waits on P1-12, P1-15, and P1-17

### 2026-10-26 P1-17 cursor
Task:      P1-17 ATT, WEB, and COMP T2 cases
Status:    blocked (T2-ATT-05, T2-COMP-01, T2-COMP-02, T2-COMP-03, T2-COMP-04)
Changed:   regression/drivers/bundles/host_tools.py installs the attachment store, load_file, a fake web search tool, and the computer tools with their inspector and persist hook. The loop driver passes the store and the persist hook into the turn. Workspace copies keep symlinks. The MCP env overlay expands {workspace} before the child starts. T2-ATT-06 skips turn_complete because the second user message is what observes the frozen descriptor. T2-COMP-06 approves the ask so check_trashable runs, and the driver plants 10001 files under ~/Downloads for that case only. No src change.
Verify:    uv run pytest regression/t2/loop/scenarios/attachments regression/t2/loop/scenarios/web regression/t2/loop/scenarios/computer -q -n auto → 13 passed, 5 failed in 7.39s. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed. uv run pytest tests -q -n auto → 3316 passed, 25 skipped, 1 failed in 73.90s. ruff check and format on the edited modules passed.
Catalog:   Test cells were already filled. No Expect text changed.
Recorded failures not fixed: test-runs/20261026T152511Z/20261026T152511Z-aa1d34b-3491.jsonl — T2-ATT-05 load_per_reply_cap.yaml 3740de1fe08d, sixth load_file returns a loaded image, no "At most 5 attachments per reply"; T2-COMP-01 open_exec_suffix_denied.yaml 4b2d52b32912, refusal is on error with error_kind null and result empty; T2-COMP-02 denied_dirs_and_globs.yaml 31cb8f8babaa, same deny shape; T2-COMP-03 denied_apps_and_schemes.yaml cb49498ad668, same deny shape; T2-COMP-04 clipboard_cap.yaml 1d3beb60e94f, ask denial "Allow this action on your computer?" and write_clipboard does not reject 20001 characters. test-runs/20261026T152746Z/20261026T152746Z-aa1d34b-13371.jsonl and test-runs/20261026T152809Z/20261026T152809Z-aa1d34b-14767.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 242, seed expects 18. Prior ledger test-runs/20261026T140128Z/20261026T140128Z-aa1d34b-94390.jsonl is superseded for the 13 cases that now pass.
Open questions: T2-ATT-05 needs a per-reply cap on load_file; today the cap only applies to attachment refs on a user reply. T2-COMP-01, 02, and 03 need inspector denials to carry error_kind policy and the sentence on result, or the expectation has to match the error field. T2-COMP-04 needs write_clipboard to reject a 20001-character write. Each is a src change or a quarantine the user adds. Source was not changed.
Next:      P1-18 waits on P1-10, P1-12, and P1-15

### 2026-10-26 P1-10 cursor
Task:      P1-10 CTX T2 cases (T2-CTX-02, 07, 09, 16, 17)
Status:    in-progress (07 and 17 pass; 02, 09, and 16 fail)
Changed:   regression/schema/assertions.py (provider request model and messages_not_contain; history blocks_include). regression/drivers/loop.py copies stream_models and the recorded search flag. regression/drivers/support/tokens.py records vertex_google_search on the scripted stream. regression/schema/steps.py rewind step. Five scenarios under regression/t2/loop/scenarios/ctx/. docs/testing.md Test cells for those five. regression/selftest/test_assertions.py and test_steps.py.
Verify:    uv run pytest regression/selftest/test_steps.py regression/selftest/test_assertions.py regression/t2/loop/scenarios/ctx -q → 68 passed, 8 failed in 1.81s. ruff check on the edited modules passed after format.
Catalog:   T2-CTX-02, 07, 09, 16, and 17 Test cells filled.
Recorded failures not fixed: test-runs/20261026T153011Z/20261026T153011Z-aa1d34b-23143.jsonl — P1-10-rewind regression/selftest/test_steps.py::test_rewind_drops_rows_after_the_anchor, history_pairs orphan c1 because rewind drops the tool request already stored; T2-CTX-09 background_summary_stale_fallback.yaml, same orphan c1; T2-CTX-02 compact_pins_verdict_attachment_thinking.yaml, blocks_include[1] text was not kept (verdict and tail thinking were kept); T2-CTX-16 tool_result_shaping_under_pressure.yaml, strict_script leftover 1 and no provider request 3; T2-CTX-04 compact_keep_tail_ratio.yaml, orphan tool id cut; T2-CTX-10 epoch_volatile_update.yaml, no SystemContextUpdated with changed_sources memory; T2-CTX-11 epoch_stable_change.yaml, no ContextEpochStarted epoch_id 2; T2-CTX-18 context_usage_current_not_peak.yaml, ContextUsage was not found in order.
Open questions: T2-CTX-02's attachment descriptor is summarized away; only the verdict row is pinned. T2-CTX-09's rewind fires on the tool result and cuts that tool pair, so the stale-summary claim was not reached. T2-CTX-16 made two provider calls, so the head/tail/spill checks never ran.
Next:      P1-18 waits on P1-10, P1-12, and P1-15

### 2026-10-26 P1-15 cursor
Task:      P1-15 MEM T2 cases (T2-MEM-01 … T2-MEM-09)
Status:    blocked (T2-MEM-02)
Changed:   regression/drivers/bundles/memory.py drains the outbox after the turn, counts outbox rows, routes search through InMemoryPalace, and rejects a shared outbox with an ephemeral palace. regression/drivers/bundles/session.py turns the writer on, skips the subsystem when memory.enabled is false, and uses the scenario sqlite DB_URL. regression/drivers/bundles/protocol.py holds boot_error and outbox_count. regression/drivers/loop.py short-circuits a boot error, slices invariants per message, and puts the outbox count on the store view. Scenarios for T2-MEM-01, 02, 04, 05, 06, 07, 08, and 09. regression/t2/python/test_memory_commit.py for T2-MEM-03. No src, testkit, or tests change.
Verify:    uv run pytest regression/t2/loop/scenarios/memory regression/t2/python/test_memory_commit.py -q -n auto → 8 passed, 1 failed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert 254 == 18. T2-MEM ids pair. Full tests suite was not run.
Catalog:   T2-MEM-01..05, 07, and 09 Test cells filled. T2-MEM-06 and T2-MEM-08 paths were already correct.
Recorded failures not fixed: test-runs/20261026T153506Z/20261026T153506Z-aa1d34b-41293.jsonl — T2-MEM-02 outbox_thread_isolation.yaml provider_request[3] system_not_contains 'alpha-thread-secret-line' was in the system prompt. test-runs/20261026T153523Z/20261026T153523Z-aa1d34b-42431.jsonl — test_catalog_pairing.py assert 254 == 18.
Open questions: Share-threads off scopes L2 recall only. Wake-up still lists every drawer in the wing, so thread B's system prompt contains thread A's line. Left failing. No src change.
Next:      user ruling on T2-MEM-02. P1-18 stays unstarted.

### 2026-10-26 P1-10 cursor
Task:      P1-10 CTX T2 cases (T2-CTX-01 … T2-CTX-18)
Status:    done
Changed:   Compaction keeps a tool response's request in the tail, and pins a row whose blocks are all verifier verdicts or attachment-descriptor text. Project instructions reload on each inner turn. The harness can set memory lines after the current request has settled, rewind on ToolInputDelta, and assert that ContextUsage drops across a summary. Scenarios for the eight failures. A repair placeholder is not treated as a stored tool result.
Verify:    uv run pytest tests/unit/core/runtime/test_history_compaction_split.py regression/selftest/test_steps.py regression/selftest/test_assertions.py regression/t2/loop/scenarios/ctx -q → 93 passed. After the repair exclusion, uv run pytest tests/unit/core/runtime/test_loop.py::test_token_pressure_trigger_agrees_with_raw_compaction tests/unit/core/runtime/test_history_compaction_split.py regression/t2/loop/scenarios/ctx -q → 38 passed in 2.15s. uv run pytest tests/unit/core/runtime tests/unit/core/context tests/unit/core/messages -q → 545 passed, then 1 failed and that failure was fixed before done. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed. ruff check on the edited modules passed.
Catalog:   Test cells were already filled. No Expect text changed.
Recorded failures not fixed: test-runs/20261026T160115Z/20261026T160115Z-aa1d34b-36405.jsonl — tests/contracts/test_catalog_pairing.py::test_catalog_pairing, filled Test cells 254, seed expects 18. Pre-existing. test-runs/20261026T155803Z/20261026T155803Z-aa1d34b-24830.jsonl — test_token_pressure_trigger_agrees_with_raw_compaction[view_only_viable], view was not viable; superseded, the repair placeholder is no longer a tail anchor.
Open questions: none
Next:      P1-18 waits on P1-12. P1-09, P1-15, and P1-17 are blocked.

### 2026-10-26 P1-09 cursor
Task:      P1-09 HOOK T2 cases (T2-HOOK-01 … T2-HOOK-15)
Status:    done
Changed:   regression/t2/python/test_hooks_lifecycle.py expected order. docs/testing.md T2-HOOK-01 Expect. regression/plan/reference/06-coverage/6C-hook.md Asserts. The loop still starts the tool before the reply-finished hook is recorded.
Verify:    uv run pytest regression/t2/loop/scenarios/hook regression/t2/python/test_hooks_lifecycle.py -q -n auto → 15 passed in 7.51s. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed. Full tests suite was not run.
Catalog:   T2-HOOK-01 Expect now says PRE_TOOL, then AFTER_PROVIDER_RESPONSE, then POST_TOOL. Test cell unchanged.
Recorded failures not fixed: test-runs/20261026T160528Z/20261026T160528Z-aa1d34b-51710.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 254, seed expects 18. Pre-existing. Prior ledger test-runs/20261026T153905Z/20261026T153905Z-aa1d34b-56059.jsonl T2-HOOK-01 is superseded.
Open questions: none
Next:      P1-18 waits on P1-12. P1-15 and P1-17 stay blocked.

### 2026-10-26 P1-12 cursor
Task:      P1-12 VER T2 cases (T2-VER-01 … T2-VER-23); retire tests/evals
Status:    done
Changed:   git mv tests/evals/test_verifier_port.py → tests/unit/core/verifier/test_verifier_port.py (same two asserts: queued verdict_id and recorded evidence). git rm tests/evals/__init__.py and removed the folder. tests/conftest.py no longer marks tests/evals as t2. No src change. No scenario change.
Verify:    uv run pytest tests/unit/core/verifier/test_verifier_port.py regression/t2/loop/scenarios/verifier regression/t2/python/test_drift_confusion_matrix.py -q -n auto → 28 passed in 8.68s. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed. uv run pytest tests -q -n auto → 3318 passed, 25 skipped, 3 failed in 74.10s.
Catalog:   no new cells. T2-VER-01..23 Test cells were already filled.
Recorded failures not fixed: test-runs/20261026T162638Z/20261026T162638Z-aa1d34b-28030.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 254, seed expects 18. Pre-existing. test-runs/20261026T162645Z/20261026T162645Z-aa1d34b-28425.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, same seed mismatch. Same run, case null, tests/unit/core/memory/test_mempalace.py::test_recall_is_thread_scoped, AssertionError: 'private other thread' is in the t1 recall. Same run, case null, tests/unit/core/memory/test_review_fixes.py::test_recall_is_thread_scoped, AssertionError: t1 recall contains 'beta secret' (assert not True). Those two memory failures are uncommitted palace/subsystem edits already in the tree, not this task.
Open questions: none. P1-18's search for tests/evals still hits the scripted reply text in regression/t2/loop/scenarios/file/tool_glob_grep.yaml, plus plan and docs history.
Next:      P1-18 waits on P1-15 and P1-17

### 2026-10-26 P1-15 cursor
Task:      P1-15 MEM T2 cases (T2-MEM-01 … T2-MEM-09)
Status:    done
Changed:   Share across chats defaults on. `MONKEYBOT_MEMORY_SHARE_THREADS=0` scopes both recall and the wake-up briefing to the current chat. Spaces Privacy & access has a Share memory across chats toggle; changing it restarts live agents. Isolation tests set the flag off.
Verify:    uv run pytest tests/unit/core/memory/test_review_fixes.py tests/unit/core/memory/test_mempalace.py regression/t2/loop/scenarios/memory -q -n auto → 64 passed. uv run pytest regression/t2/python/test_memory_commit.py -q → 1 passed. pnpm test of tests/main/settings.share-chat-memory.test.ts → 2 passed.
Catalog:   T1-MEM-01 Expect now says the default is on.
Recorded failures not fixed: none from this change. The earlier T2-MEM-02 leak is fixed. test_catalog_pairing 254 == 18 stays pre-existing.
Open questions: none
Next:      P1-18 waits on P1-17

### 2026-10-26 P1-17 cursor
Task:      P1-17 ATT, WEB, and COMP T2 cases
Status:    blocked (T2-COMP-04 clipboard write cap)
Changed:   A hard computer denial carries error_kind policy. Scenarios for T2-COMP-01..03 match that sentence on the error field. T2-ATT-05 expects six load_file calls to succeed. docs/testing.md and the ATT and COMP coverage rows match those rulings. Clipboard write is unchanged.
Verify:    uv run pytest tests/unit/computer/test_permissions.py load_per_reply_cap.yaml open_exec_suffix_denied.yaml denied_dirs_and_globs.yaml denied_apps_and_schemes.yaml move_trash_not_always_scoped.yaml trash_huge_tree_refused.yaml -q → 22 passed in 1.51s. ruff check and format on the edited modules passed.
Catalog:   T2-ATT-05 Expect now says six load_file calls succeed and the five-attachment cap applies to a user's reply. T2-COMP-02 and T2-COMP-03 Expect now say error_kind policy. Test cells unchanged.
Recorded failures not fixed: T2-COMP-04 clipboard_cap.yaml was not re-run. Prior ledger test-runs/20261026T152511Z/20261026T152511Z-aa1d34b-3491.jsonl, T2-COMP-04, write_clipboard does not reject 20001 characters and the ask fires first. T2-ATT-05 and T2-COMP-01..03 from that run are superseded.
Open questions: Whether a 20001-character clipboard write should be refused. Recommendation: no. The 20000 limit is a read cap so the agent does not swallow the clipboard. A write the user approves should land in full.
Next:      P1-18 waits on the clipboard ruling

### 2026-10-26 P1-17 cursor
Task:      P1-17 ATT, WEB, and COMP T2 cases
Status:    done
Changed:   regression/t2/loop/scenarios/computer/clipboard_cap.yaml approves the ask and expects chars_written 20001. docs/testing.md T2-COMP-04 Expect. regression/plan/reference/06-coverage/6I-comp.md Asserts. write_clipboard was not changed.
Verify:    uv run pytest regression/t2/loop/scenarios/attachments regression/t2/loop/scenarios/web regression/t2/loop/scenarios/computer -q -n auto → 18 passed in 7.41s.
Catalog:   T2-COMP-04 Expect now says a 20001-character write asks, then writes the full text once approved. The 20000 cap applies to reads. Test cell unchanged.
Recorded failures not fixed: test-runs/20261026T164108Z/20261026T164108Z-aa1d34b-81291.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 254, seed expects 18. Pre-existing. Prior ledger test-runs/20261026T152511Z/20261026T152511Z-aa1d34b-3491.jsonl T2-COMP-04 is superseded.
Open questions: none
Next:      P1-18

### 2026-10-26 P1-18 cursor
Task:      P1-18 STORE and OPS (T2-STORE-01, 02; T2-OPS-01, 02, 04 … 06); phase exit
Status:    done
Changed:   regression/drivers/bundles/store.py stamps preserved history row ids and records one turn_usage row per TurnComplete. regression/drivers/bundles/protocol.py holds usage_backend. regression/drivers/loop.py applies that usage snapshot. Scenarios compaction_reset_row_ids.yaml and usage_recorded_on_turn_complete.yaml. regression/t2/python/test_ops_cli.py and test_ops_observability.py. docs/testing.md Test cells for those seven cases. No src, testkit, or tests change. tests/evals was already gone.
Verify:    uv run pytest regression/t2/loop/scenarios/store regression/t2/python/test_ops_cli.py regression/t2/python/test_ops_observability.py -q -n auto → 7 passed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed, assert 261 == 18. Pairing violations are 0, including the new ids. Full tests suite was not run.
Catalog:   T2-STORE-01, T2-STORE-02, T2-OPS-01, T2-OPS-02, T2-OPS-04, T2-OPS-05, and T2-OPS-06 Test cells filled. T2-STORE-03, T2-OPS-03, and the T2-*-G rows stay empty; they belong to later phases.
Exit check: tests/evals is not on disk. rg of tests/evals under tests and regression still hits regression/plan history and the scripted reply in regression/t2/loop/scenarios/file/tool_glob_grep.yaml. Phase 6 owns those leftover path mentions. They were left.
Recorded failures not fixed: test-runs/20261026T171526Z/20261026T171526Z-aa1d34b-5266.jsonl — test_catalog_pairing.py assert 261 == 18. Known seed false failure.
Open questions: T2-OPS-05 uses testkit/subagent_worker.py for the spawn. That worker does not start spans. The child span is span_subagent attached to the traceparent the parent stored on the envelope.
Next:      Phase 2

