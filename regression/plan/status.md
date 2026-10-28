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
- **Last update:** 2026-10-27, P6-01 done. Phase 5 is deferred. The local gate passed. Live smoke was not run.
- **Next task:** none. Phase 5 stays deferred until the user resumes it.
- **Deferred cases:** Phase 5 (P5-02, P5-03). T4-RT-01 stays an empty live cell (GCP credentials). T3-PROV-01..05 and the non-Ollama families of T3-PROV-06 stay empty (D13).

## Phases

| Phase | Name | Tasks | Done | Status | Exit check | Completed |
|---|---|---|---|---|---|---|
| [0a](phases/phase-0a-tier1-reorg.md) | Tier-1 reorganization and failure ledger | 6 | 6 | done | P0A-06 | 2026-10-25 |
| [0b](phases/phase-0b-foundations.md) | Foundations and docs/testing.md | 7 | 7 | done | P0B-07 | 2026-10-25 |
| [1](phases/phase-1-t2-harness-logic.md) | T2 harness logic | 18 | 18 | done | P1-18 | 2026-10-26 |
| [2](phases/phase-2-contracts-and-gateway.md) | Contracts and gateway | 10 | 10 | done | all P2 tasks done | 2026-10-26 |
| [3](phases/phase-3-record-replay.md) | Record / replay | 4 | 4 | done | all P3 tasks done | 2026-10-27 |
| [4](phases/phase-4-live.md) | Live tier | 6 | 6 | done | P4-06 | 2026-10-26 |
| [5](phases/phase-5-trials.md) | Trials and baselines | 3 | 1 | deferred | P5-03 | |
| [6](phases/phase-6-cutover.md) | Cutover | 1 | 1 | done | P6-01 | 2026-10-27 |

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
| P2-01 | done | cursor | 2026-10-26 | uncommitted | gateway scenarios run in-process; YAML collector still calls the loop driver |
| P2-02 | done | cursor | 2026-10-26 | uncommitted | SSE schema and both golden streams pass; gateway YAML dispatch wired |
| P2-03 | done | cursor | 2026-10-26 | uncommitted | Appendix D uses the live parameter names and lists the docs routes |
| P2-04 | done | cursor | 2026-10-26 | uncommitted | a database path edit stays on the open database and reports restart |
| P2-05 | done | cursor | 2026-10-26 | uncommitted | interrupt commits the partial assistant reply |
| P2-06 | done | cursor | 2026-10-26 | uncommitted | Gemini pairs tool results, joins argument fragments, and emits usage after tools |
| P2-07 | done | cursor | 2026-10-26 | uncommitted | second embedding migrator skips while the lock is held |
| P2-08 | done | cursor | 2026-10-26 | uncommitted | s3 memory rejected; tool errors use the envelope; corrupt grants stay on disk; setup-script export removed |
| P2-09 | done | cursor | 2026-10-26 | uncommitted | hung embedder drain returns and leaves the row pending |
| P2-10 | done | cursor | 2026-10-26 | uncommitted | T3-GW-02 fails on stand-in drift; T3-GW-03 passes |
| P3-01 | done | cursor | 2026-10-26 | uncommitted | cassette model, from_transcript, loop-driver replay, T2-STORE-03 |
| P3-02 | done | cursor | 2026-10-27 | uncommitted | five cassettes replay; T3-LOOP-02 recorded on gemma4:31b |
| P3-03 | done | cursor | 2026-10-27 | uncommitted | Ollama wire cassettes replay; staleness check in ci-local.sh and ci.yml |
| P3-04 | done | cursor | 2026-10-26 | uncommitted | gateway replay, cassette_turn.sse, MCP, child-tape and wake-up recordings; T3-GW-01, T3-MCP-01, T3-SUB-01, T3-MEM-01 |
| P4-01 | done | cursor | 2026-10-26 | uncommitted | evals code in regression/t4 and t5; fixtures in workspaces/; six selftests plus a `live` test_judge |
| P4-02 | done | cursor | 2026-10-26 | uncommitted | key from env or repo-root .env (user decision); missing key exits 2 with a run_error; live preflight passed 2026-10-26 (user confirmed) |
| P4-03 | done | cursor | 2026-10-26 | uncommitted | 29 scenarios with case IDs under file/ and shell/; plugin collects t4 items; 15 Test cells filled |
| P4-04 | done | cursor | 2026-10-26 | uncommitted | seven live cases and scheduler.yaml; runner groups by config; `--suite` filters python tests |
| P4-05 | done | cursor | 2026-10-26 | uncommitted | secret gate and provider-key envs removed; paths point at regression/t4; NVIDIA judge pin dropped, so the judge comes from eval_config.yaml |
| P4-06 | done | cursor | 2026-10-26 | uncommitted | smoke 12/12 passed; baseline drift recorded, not fixed |
| P5-01 | done | cursor | 2026-10-27 | uncommitted | pass^N and Wilson compare to keyed baselines; T5 Test cells except GW |
| P5-02 | deferred | cursor | 2026-10-27 | uncommitted | user deferred Phase 5; live baseline processes stopped. subagents baseline file exists |
| P5-03 | deferred | cursor | 2026-10-27 | uncommitted | user deferred Phase 5. Storm tests exist and failed; compare not run |
| P6-01 | done | cursor | 2026-10-27 | uncommitted | cutover gate passed; live smoke not run; Phase 5 deferred |

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

### 2026-10-26 P2-08 cursor
Task:      P2-08 Cross-cutting T1 contracts
Status:    blocked (four cases disagree with the product)
Changed:   tests/contracts/test_config_matrix.py, test_harness_prompt_golden.py, test_tool_error_catalog.py, test_policy_decision_table.py, test_scaffold_reexports.py, test_span_names.py; tables config_precedence.yaml and policy_decisions.yaml (47 rows); goldens prompts/*, tool_error_codes.txt, span_names.txt; docs/testing.md Test cells for the 15 cases. No src change.
Verify:    uv run pytest on the six modules → 4 failed, 68 passed (test-runs/20261026T190041Z/20261026T190041Z-1b053d9-17937.jsonl). Pairing violations 0; seed still 261/269 while the catalog is 283 filled and 291 paths (test-runs/20261026T190210Z/20261026T190210Z-1b053d9-18158.jsonl). Seed not edited while other tasks are still filling cells.
Catalog:   T1-CTX-01, T1-CTX-02, T1-FILE-01, T1-FILE-02, T1-POL-01, T1-POL-02, T1-POL-03, T1-CFG-01..06, T1-OPS-01, T1-OPS-02
Recorded failures not fixed: T1-CFG-04 test_memory_backend_s3_rejected, memory.backend s3 does not raise ConfigError. T1-FILE-02 test_builtin_tool_errors_use_the_envelope, error_kind pending at core_tool_executor.py:2003 plus plain-string errors in disable_mcp, MCP meta/resource/prompt, loops, goals, and ask_user. T1-POL-03 test_corrupt_grants_load_empty_and_log_once, bad grants.json loads empty and logs 0 times. T1-OPS-01 test_scaffold_reexports_exist_on_cli, install_setup_script is missing from monkeybot_cli.scaffold.
Open questions: none. Product code was not changed.
Next:      user review of the four blocked cases

### 2026-10-26 P2-09 cursor
Task:      P2-09 Single-package T1 additions
Status:    blocked (T1-MEM-02)
Changed:   tests/unit/core/context/test_skill_discovery_rules.py, tests/unit/core/memory/test_memory_scoping.py, tests/unit/core/subagents/test_child_tool_list.py, tests/unit/computer/test_platform_gate.py, new functions in tests/unit/scheduler/test_interval.py. docs/testing.md Test cells for the seven cases. No src change.
Verify:    uv run pytest on those five modules → 13 passed, 1 failed (test-runs/20261026T190219Z/20261026T190219Z-1b053d9-18206.jsonl). Pairing failed only on the seed (283 filled, 291 paths).
Catalog:   T1-SKILL-01, T1-MEM-01, T1-MEM-02, T1-MEM-03, T1-SUB-01, T1-SCHED-01, T1-COMP-01
Recorded failures not fixed: T1-MEM-02 tests/unit/core/memory/test_memory_scoping.py::test_hung_embedder_rows_stay_pending, drain_writer(timeout_s=0.2) did not return while the embedder stayed blocked, so the row was not left pending.
Open questions: none. Product code was not changed.
Next:      user review of T1-MEM-02

### 2026-10-26 P2-07 cursor
Task:      P2-07 Storage parity and migrations
Status:    blocked (T1-STORE-23)
Changed:   testkit/backends.py; tests/contracts/conftest.py (open_backend fixture); tests/contracts/test_storage_parity.py; tests/contracts/test_migrations_replay.py; tests/contracts/fixtures/legacy_db/{pre_row_id,pre_agent_scope,pre_branch_pk,pre_usage_cache_cols}.sqlite; docs/testing.md Test cells for T1-STORE-01..14 and T1-STORE-20..25. No src change.
Verify:    uv run pytest tests/contracts/test_storage_parity.py tests/contracts/test_migrations_replay.py -q → 1 failed, 20 passed, 29 skipped, 1 xfailed (test-runs/20261026T190751Z/20261026T190751Z-1b053d9-19431.jsonl). Postgres and Firestore skipped. Pairing violations 0; seed still 261/269 while the catalog is 303 filled and 311 paths. Seed not edited.
Catalog:   T1-STORE-01..14 → test_storage_parity.py; T1-STORE-20..25 → test_migrations_replay.py
Schema SHAs: pre_row_id b4378f48eb50969d5d1900ba071ab7a2271a8457; pre_agent_scope 16a9f637e343a7c99efe5b567c69862abd95d1f9; pre_usage_cache_cols 3d1e4f488c6bb038928fdcbc8451708ca94a2cb0; pre_branch_pk 164b76cdc3708ad573e993cb24aa725e2bb13cca
Recorded failures not fixed: T1-STORE-23 tests/contracts/test_migrations_replay.py::test_embedding_migrate_lock, second migrator blocked instead of skipping while the lock was held. T1-STORE-21 stays xfail(strict=True) because the catalog says create_schema.py misses the _ensure_* upgrades.
Open questions: none. Product code was not changed.
Next:      user review of T1-STORE-23

### 2026-10-26 P2-01 cursor
Task:      P2-01 Gateway harness, streaming SSE reader, gateway driver
Status:    done
Changed:   testkit/sse.py, testkit/gateway.py, regression/drivers/gateway.py, regression/fixtures/scripts/one_turn.yaml, regression/schema/steps.py (disconnect_sse, reconnect_sse), tests/unit/testkit/test_sse_reader.py, regression/selftest/test_gateway_driver.py. No src change.
Verify:    uv run pytest tests/unit/testkit/test_sse_reader.py regression/selftest/test_gateway_driver.py regression/selftest -q → 198 passed in 1.38s
Catalog:   n/a (no new case rows)
Recorded failures not fixed: none
Open questions: regression/plugin.py ScenarioItem.runtest still always calls the loop driver. A driver: gateway YAML will not reach the gateway driver until dispatch is wired. P2-02 owns that wiring.
Next:      P2-02, P2-03, P2-05

### 2026-10-26 P2-06 cursor
Task:      P2-06 Provider conformance (T1-PROV-01 … 10)
Status:    blocked (Gemini disagrees with the catalog)
Changed:   tests/contracts/test_provider_conformance.py, tests/contracts/tables/provider_error_mapping.yaml, testkit/conversations.py (canonical_conversation), docs/testing.md Test cells for T1-PROV-01..10. No src change.
Verify:    uv run pytest tests/contracts/test_provider_conformance.py -q -n auto → 65 passed, 5 failed (test-runs/20261026T191019Z/20261026T191019Z-1b053d9-19729.jsonl). Pairing violations 0; seed still 261/269 while the catalog is 313 filled and 321 paths (test-runs/20261026T191225Z/20261026T191225Z-1b053d9-20387.jsonl). Seed not edited.
Catalog:   T1-PROV-01..10 → tests/contracts/test_provider_conformance.py
Recorded failures not fixed: T1-PROV-01 test_converter_conformance[gemini], function_response ids are [None, None], not ['c-echo', 'c-ls']. T1-PROV-02 truncated-gemini, ({}, None) instead of a malformed-JSON parse error. T1-PROV-02 json-array-gemini, TypeError instead of 'tool args must be a JSON object, got list'. T1-PROV-02 split-mid-string-gemini, ({}, None) instead of ({'q': 'hello'}, None). T1-PROV-03 gemini, event order is text, thinking, usage, tool, done, not text, thinking, tool, usage, done.
Open questions: none. Product code was not changed.
Next:      user review of the five Gemini failures

### 2026-10-26 P2-03 cursor
Task:      P2-03 Route table and error envelope (T1-GW-05, 06)
Status:    blocked (T1-GW-05)
Changed:   tests/contracts/test_route_table.py, tests/contracts/goldens/routes.txt, docs/testing.md Test cells for T1-GW-05 and T1-GW-06. Appendix D and src were not edited.
Verify:    uv run pytest tests/contracts/test_route_table.py -q → 1 failed, 1 passed (test-runs/20261026T192234Z/20261026T192234Z-1b053d9-21254.jsonl). Pairing violations 0; seed still 261/269 while the catalog is 315 filled and 323 paths (test-runs/20261026T192234Z/20261026T192234Z-1b053d9-21253.jsonl). Seed not edited.
Catalog:   T1-GW-05 and T1-GW-06 → tests/contracts/test_route_table.py
Recorded failures not fixed: T1-GW-05 tests/contracts/test_route_table.py, app.routes disagrees with Appendix D (24 missing, 32 extra). Path parameters are {session_id}, {loop_id}, and {goal_id} where Appendix D writes {id}. FastAPI also mounts GET/HEAD /openapi.json, /docs, /docs/oauth2-redirect, and /redoc. T1-GW-06 passed.
Open questions: none. Product code was not changed.
Next:      user review of T1-GW-05

### 2026-10-26 P2-02 cursor
Task:      P2-02 SSE schema contract and golden streams
Status:    done
Changed:   tests/contracts/test_sse_schema.py; tests/contracts/goldens/sse/schema.json; regression/t3/sse/basic_turn.sse and tool_turn.sse; regression/t2/gateway/scenarios/gw/scripted_turn_golden_sse.yaml and tool_turn_golden_sse.yaml; regression/schema/sse_norm.py, invariants.py, assertions.py; regression/drivers/gateway.py; regression/plugin.py (driver: gateway branch); tests/unit/gateway/sse/test_session_bus.py now calls event_to_json; docs/testing.md Test cells for T1-GW-01..04 and T2-GW-01..02; pyproject.toml and uv.lock add jsonschema as a dev dependency. No src change.
Verify:    uv run pytest tests/contracts/test_sse_schema.py -q → 4 passed. uv run pytest regression/t2/gateway/scenarios/gw -q → 2 passed. Pairing violations 0; seed still 261/269 while the catalog is 321 filled and 329 paths (test-runs/20261026T192742Z/20261026T192742Z-1b053d9-22023.jsonl). Seed not edited.
Catalog:   T1-GW-01..04 → tests/contracts/test_sse_schema.py. T2-GW-01 → scripted_turn_golden_sse.yaml. T2-GW-02 → tool_turn_golden_sse.yaml.
Recorded failures not fixed: none
Open questions: none
Next:      P2-04

### 2026-10-26 P2-05 cursor
Task:      P2-05 Realtime driver (T2-RT-01 … 07, T1-RT-01)
Status:    blocked (T2-RT-04)
Changed:   regression/drivers/realtime.py; regression/plugin.py (driver: realtime branch); regression/fixtures/configs/permissions.ask.yaml; regression/t2/realtime/scenarios/ws_{text_turn,tool_turn,tool_confirmation,interrupt,slot_limit,guardrail_budget,blocks_history_rewrite}.yaml; tests/contracts/test_realtime_divergence.py; docs/testing.md Test cells for T1-RT-01 and T2-RT-01..07. schema already had driver: realtime, client_text, and interrupt. No src change. T4-RT-01 stays empty.
Verify:    uv run pytest regression/t2/realtime tests/contracts/test_realtime_divergence.py regression/selftest -q → 1 failed, 203 passed (test-runs/20261026T194658Z/20261026T194658Z-1b053d9-36896.jsonl). Pairing violations 0; seed still 261/269 while the catalog is 329 filled and 337 paths (test-runs/20261026T194820Z/20261026T194820Z-1b053d9-39627.jsonl). Seed not edited.
Catalog:   T1-RT-01 and T2-RT-01..07 filled.
Recorded failures not fixed: T2-RT-04 regression/t2/realtime/scenarios/ws_interrupt.yaml, history roles [] != ['user', 'assistant']. The wire is connect, connected, text, text_delta, interrupt, interrupted. Interrupt discards the assistant buffer, and history is committed only on an assistant boundary, so the partial reply is not stored.
Open questions: none. Product code was not changed.
Next:      user review of T2-RT-04

### 2026-10-26 P2-09 cursor
Task:      P2-09 Single-package T1 additions (T1-SKILL-01, T1-MEM-01 … 03, T1-SUB-01, T1-SCHED-01, T1-COMP-01)
Status:    done
Changed:   src/monkeybot/core/memory/writer.py drain bounds the in-flight embed and releases the claim on timeout. src/monkeybot/core/memory/outbox.py, src/monkeybot/core/persistence/postgres.py, and src/monkeybot/core/persistence/firestore.py gain release_processing. No test change.
Verify:    uv run pytest tests/unit/core/context/test_skill_discovery_rules.py tests/unit/core/memory/test_memory_scoping.py tests/unit/core/subagents/test_child_tool_list.py tests/unit/computer/test_platform_gate.py tests/unit/scheduler/test_interval.py -q → 14 passed. Memory writer suites test_memory_scoping.py, test_review_fixes.py, test_mempalace.py → 60 passed.
Catalog:   T1-MEM-02 Test cell unchanged. The case now passes.
Recorded failures not fixed: none from this change. Prior T1-MEM-02 hang is superseded.
Open questions: none
Next:      P2-04 and P2-10 are in progress

### 2026-10-26 P2-10 cursor
Task:      P2-10 monkeyapp contract (T3-GW-02, 03), cross-repo
Status:    done
Changed:   monkeyapp: scripts/vendor-sse-schema.mjs; tests/contracts/sse/schema.json (header names monkeybot 1b053d9b86ba9f049f005392ab440b722aecb4d5); tests/unit/sse-schema.ts, fake-gateway-frames.test.ts, dispatch-gateway-cases.test.ts; package.json and pnpm-lock.yaml add ajv; .prettierignore excludes the vendored schema. monkeybot: tests/contracts/test_catalog_pairing.py accepts monkeyapp: Test cells when ../monkeyapp exists and skips them with a reason otherwise; docs/testing.md Test cells for T3-GW-02 and T3-GW-03. The stand-in gateway was not changed.
Verify:    pnpm exec node --import ./tests/register.mjs --test tests/unit/dispatch-gateway-cases.test.ts tests/unit/fake-gateway-frames.test.ts → 1 passed, 1 failed. Pairing monkeyapp paths add no violations. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 failed on the seed.
Catalog:   T3-GW-02 → monkeyapp:tests/unit/fake-gateway-frames.test.ts. T3-GW-03 → monkeyapp:tests/unit/dispatch-gateway-cases.test.ts.
Recorded failures not fixed: T3-GW-02 tests/unit/fake-gateway-frames.test.ts, stand-in frames disagree with the vendored schema. TurnComplete missing usage (31) and sometimes request_id (3). AssistantDelta missing request_id (20). ContextSummarized missing request_id, and one frame also has estimated_tokens. ContextUsage and ContextSummarizing missing request_id. ToolConfirmationRequest missing request_id and prompt, and has tool. ToolCallStarted missing label (10). ToolCallResult missing error (10). ImageBlock has filename (3). HistoryRewritten missing request_id (5). T3-GW-03 passed. test-runs/20261026T201046Z/20261026T201046Z-1b053d9-91961.jsonl, case null, tests/contracts/test_catalog_pairing.py::test_catalog_pairing, AssertionError: filled Test cells 331, seed expects 261. Pre-existing seed. P2-04 gateway citations are not in Test cells yet.
Open questions: none. Fixing the stand-in gateway is a separate monkeyapp task.
Next:      P2-04

### 2026-10-26 P2-07 cursor
Task:      P2-07 Storage parity and migrations
Status:    done
Changed:   src/monkeybot/core/memory/migrate_embedding.py takes the palace lock without waiting. A busy lock returns action skipped_locked and leaves the config unchanged. No test change.
Verify:    uv run pytest tests/contracts/test_storage_parity.py tests/contracts/test_migrations_replay.py tests/unit/core/memory/test_migrate_embedding.py -q → 29 passed, 29 skipped, 1 xfailed. Postgres and Firestore skipped. T1-STORE-23 passes. T1-STORE-21 stays xfail(strict=True).
Catalog:   T1-STORE-23 Test cell unchanged. The case now passes.
Recorded failures not fixed: T1-STORE-21 tests/contracts/test_migrations_replay.py::test_create_schema_matches_apply_schema, xfail(strict=True) because scripts/create_schema.py misses the _ensure_* upgrades and branch rebuild.
Open questions: none
Next:      P2-04 is in progress

### 2026-10-26 P2-04 cursor
Task:      P2-04 Remaining gateway cases
Status:    blocked (T2-CFG-G2)
Changed:   regression/t2/gateway/harness.py, regression/t2/gateway/test_cases.py, regression/t2/gateway/scenarios/gw/reconnect_last_event_id.yaml, regression/t2/python/test_sse_stream_pressure.py, regression/t2/python/test_ops_cli.py, testkit/gateway.py, docs/testing.md Test cells for the 30 cases. No src change.
Verify:    uv run pytest regression/t2/gateway regression/t2/python/test_sse_stream_pressure.py regression/t2/python/test_ops_cli.py -q -n auto → 1 failed, 35 passed in 7.80s (test-runs/20261026T202138Z/20261026T202138Z-1b053d9-35844.jsonl). Pairing violations 0; seed still 261/269 while the catalog is 361 filled and 370 paths (test-runs/20261026T202138Z/20261026T202138Z-1b053d9-35843.jsonl). Seed not edited.
Catalog:   T2-GW-03..21, T2-ATT-G1, T2-ATT-G2, T2-SCHED-G1, T2-SCHED-G2, T2-STORE-G1, T2-CFG-G1..G5, T2-OPS-03
Recorded failures not fixed: T2-CFG-G2 regression/t2/gateway/test_cases.py, 'DB_URL' not in restart_required []. Editing paths.db_url and posting /admin/config/reload publishes revision 2 and leaves the booted database in place, so restart_required stays empty.
Open questions: none. Product code was not changed.
Next:      user review of T2-CFG-G2. P2-08 is still in progress.

### 2026-10-26 P2-06 cursor
Task:      P2-06 Provider conformance (T1-PROV-01 … 10)
Status:    done
Changed:   src/monkeybot/providers/gemini.py sets FunctionResponse.id, concatenates string argument fragments and parses them once, and yields tool calls before usage. tests/contracts/test_provider_conformance.py returns that parse error from the Gemini fragment helper.
Verify:    uv run pytest tests/contracts/test_provider_conformance.py tests/unit/core/messages/test_gemini_history_replay.py tests/unit/providers/test_gemini_history.py -q → 87 passed.
Catalog:   T1-PROV-01..10 Test cells unchanged. The five Gemini failures now pass.
Recorded failures not fixed: none from this change.
Open questions: none
Next:      P2-08 is in progress

### 2026-10-26 P2-05 cursor
Task:      P2-05 Realtime driver (T2-RT-01 … 07, T1-RT-01)
Status:    done
Changed:   src/monkeybot/core/runtime/utterance_buffer.py interrupt keeps spoken assistant text and drops undispatched tool calls. src/monkeybot/gateway/realtime/session.py returns that fragment. src/monkeybot/gateway/realtime/routes.py commits it through the assistant-boundary path before stopping the provider. tests/unit/core/runtime/test_utterance_buffer.py expects the kept text.
Verify:    uv run pytest regression/t2/realtime tests/contracts/test_realtime_divergence.py regression/selftest -q → 204 passed. uv run pytest tests/unit/core/runtime/test_utterance_buffer.py tests/unit/gateway/realtime/test_session.py tests/unit/gateway/realtime/test_routes_client_text.py -q → 25 passed.
Catalog:   T2-RT-04 Test cell unchanged. The case now passes.
Recorded failures not fixed: none from this change. Prior T2-RT-04 empty history is superseded.
Open questions: none
Next:      P2-08 is in progress

### 2026-10-26 P2-08 cursor
Task:      P2-08 Cross-cutting T1 contracts
Status:    done
Changed:   validation.py rejects memory.backend other than chroma. core_tool_executor.py wraps MCP, loop, goal, and ask_user failures in the tool-error envelope. pending stays a kind and is listed in the contract. grant_store.py warns once per damaged file and refuses to rewrite it. monkeybot.scaffold no longer imports install_setup_script. docs/testing.md and 6D-file.md include pending. Unit tests expect the envelope and an unchanged corrupt grants file.
Verify:    uv run pytest tests/contracts/test_config_matrix.py tests/contracts/test_tool_error_catalog.py tests/contracts/test_policy_decision_table.py tests/contracts/test_scaffold_reexports.py tests/contracts/test_harness_prompt_golden.py tests/contracts/test_span_names.py tests/unit/core/tools/test_grant_store.py tests/unit/core/config/test_config_extensions.py tests/unit/core/goals/test_goals.py regression/t2/loop/scenarios/scheduler/create_goal_requires_slash_goal.yaml -q → passed. The four previously failing cases pass.
Catalog:   T1-FILE-02 Expect now includes pending. Other Test cells unchanged.
Recorded failures not fixed: none from this change. The four blocked cases are superseded.
Open questions: none
Next:      P2-03 and P2-04 stay blocked

### 2026-10-26 P2-03 cursor
Task:      P2-03 Route table and error envelope (T1-GW-05, 06)
Status:    done
Changed:   regression/plan/reference/appendices.md Appendix D. Session, loop, and goal placeholders match the live names. The eight FastAPI documentation routes are listed. No src change. routes.txt was already the live set.
Verify:    uv run pytest tests/contracts/test_route_table.py -q → 2 passed in 0.66s
Catalog:   T1-GW-05 and T1-GW-06 Test cells unchanged. Both cases pass.
Recorded failures not fixed: none from this change. Prior T1-GW-05 route mismatch is superseded.
Open questions: none
Next:      P2-04 stays blocked

### 2026-10-26 P2-04 cursor
Task:      P2-04 Remaining gateway cases
Status:    done
Changed:   src/monkeybot/core/config/snapshot.py reports a restart-tier key when the file value differs from the layout overlay and keeps the overlay. testkit/gateway.py can boot with DB_URL unset so paths.db_url is the setting in force. regression/t2/gateway/test_cases.py T2-CFG-G2 uses that. tests/unit/core/config/test_runtime_config_snapshot.py covers the overlay case.
Verify:    uv run pytest regression/t2/gateway regression/t2/python/test_sse_stream_pressure.py regression/t2/python/test_ops_cli.py tests/unit/core/config/test_runtime_config_snapshot.py tests/unit/gateway/sse/test_config_reload.py -q -n auto → 126 passed in 8.89s
Catalog:   T2-CFG-G2 Test cell unchanged. The case now passes.
Recorded failures not fixed: none from this change. Prior T2-CFG-G2 empty restart_required is superseded. test-runs/20261026T203600Z/20261026T203600Z-1b053d9-93104.jsonl was the run before the overlay was reported.
Open questions: none
Next:      P3-01

### 2026-10-26 P3-01 cursor
Task:      P3-01 Cassette model, from_transcript, replay driver (T2-STORE-03)
Status:    done
Changed:   regression/t3/tools/cassette.py (model, digests, staleness), regression/t3/tools/from_transcript.py (transcript to cassette, scrubbed, CLI), regression/drivers/replay.py (loop-driver replay, transcript roundtrip), testkit/provider.py scripted_from_cassette, regression/plugin.py (collects regression/t3/trajectories/**/*.yaml, dispatches mode: replay, --record sets and restores MONKEYBOT_RECORD_CASSETTES), regression/t2/loop/scenarios/store/transcript_convertible.yaml, regression/t3/test_replay_trajectories.py, regression/selftest/test_from_transcript.py. Also regression/t3/__init__.py and regression/t3/tools/__init__.py (package markers) and the seed counts in tests/contracts/test_catalog_pairing.py (361/370 to 362/371). No src change.
Verify:    uv run pytest regression/selftest/test_from_transcript.py regression/t3/test_replay_trajectories.py -q → 31 passed. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4063 passed, 54 skipped, 1 xfailed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run mypy src/monkeybot testkit → no issues. uv run ruff format --check . → clean.
Catalog:   T2-STORE-03 Test cell filled with regression/t2/loop/scenarios/store/transcript_convertible.yaml. No T3 rows filled here.
Recorded failures not fixed: none from this change. uv run ruff check testkit tests regression reports 33 existing findings in 13 files this task did not touch (regression/t2/python/test_ops_observability.py and 12 files under tests/unit).
Open questions: done.truncated is recoverable from a transcript only for a truncated tool batch; a text-only truncation is not stored (matters for T3-LOOP-02 in P3-02). Gateway replay raises a pointer to P3-04.
Next:      P3-04

### 2026-10-26 P3-04 cursor
Task:      P3-04 Golden SSE from a cassette; MCP, subagent and wake-up replays (T3-GW-01, T3-MCP-01, T3-SUB-01, T3-MEM-01)
Status:    done
Changed:   regression/drivers/replay.py: `driver: gateway` replay runs the cassette through the in-process SSE gateway; the staleness check and digest warning also sit on the gateway's scripted provider; the P3-04 stub error is gone. regression/t3/test_golden_sse.py with regression/t3/sse/cassette_turn.sse. regression/t3/test_replay_mcp.py with regression/t3/mcp/tools_list_call.json. regression/t3/test_replay_subagent_tape.py with regression/t3/subagents/child_tape.ndjson. regression/t3/test_replay_wake_up.py with regression/t3/memory/wake_up_snapshot.json. regression/t3/test_replay_trajectories.py: test_gateway_replay_waits_for_p3_04 became test_the_realtime_driver_does_not_replay (the stub it checked is replaced). docs/testing.md Test cells for the four rows. tests/contracts/test_catalog_pairing.py seed counts 362/371 to 366/375. No src change.
Verify:    uv run pytest regression/t3 -q → 30 passed. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4078 passed, 54 skipped, 1 xfailed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run mypy src/monkeybot testkit → no issues. uv run ruff format --check . → clean. uv run ruff check testkit tests regression → 33 findings, all in files this task did not touch (same count as P3-01). uv run python -m testkit.failures latest → failures=0.
Catalog:   T3-GW-01 → regression/t3/test_golden_sse.py. T3-MCP-01 → regression/t3/test_replay_mcp.py. T3-SUB-01 → regression/t3/test_replay_subagent_tape.py. T3-MEM-01 → regression/t3/test_replay_wake_up.py.
Recorded failures not fixed: none from this change. A run of the same suite inside the Cursor sandbox failed 24 tests (macOS jail bootstrap, git env isolation, trash, clipboard, background MCP reaping) and none under regression/t3; the unsandboxed run is clean. Run file test-runs/20261026T212531Z/20261026T212531Z-8248de8-55327.jsonl.
Open questions: (1) No T3a cassette is recorded yet (P3-02 is blocked), so T3-GW-01 builds its cassette inline in the test and writes it to a temp file. When P3-02 lands, a recorded cassette can be added as a second input to the same golden test. (2) The sharing-on half of T3-MEM-01 rebuilds a Chroma palace and needs the optional `memory` extra (`pytest.importorskip("mempalace")`). scripts/ci-local.sh and ci.yml sync without `--extra memory`, so on a clean sync that test skips and only the sharing-off half runs. Options: add `--extra memory` to the gate syncs, or accept the skip. (3) The MCP recording was made against evals/smoke_agent/fixture_mcp_server.py; the test also looks for regression/fixtures/mcp/fixture_mcp_server.py, where P4-02 moves it. (4) "Recording matches a fresh local run" tests fail when the mcp SDK or mempalace version changes (the version is in the file) or a serializer changes; that is the intended re-record signal. (5) mempalace keeps write locks under the home directory; the wake-up tests point HOME at a temp directory. Two probe runs before that fix may have added lock files under `~/.mempalace/locks` on this machine; the directory already held many.
Next:      P4-01. P3-02 and P3-03 stay blocked (P4-03 / P4-02 and the user's live confirmation); phase 3 is not done.

### 2026-10-26 P4-01 cursor
Task:      P4-01 Move the `evals/` code into `regression/t4/` and the selftests into `regression/selftest/`
Status:    done
Changed:   Moved with git mv: evals runner, scenario_loader, models, persistence, diff, report, eval_config.yaml to regression/t4/; scorer.py to regression/t4/score.py; trials.py to regression/t5/trials.py; scenarios/ and suites/ to regression/t4/ (unconverted); baselines/smoke.json and smoke.md to regression/t4/baselines/; fixtures/coding/{calc_repo,calc_repo_green,shop_repo,overlays} to regression/fixtures/workspaces/; six evals/test_*.py to regression/selftest/. evals/test_assertions.py was merged into regression/selftest/test_assertions.py (all 36 old cases kept (41 existing + 36 = 77); the old helpers are `_eval_run`, `_eval_scenario`) and removed; evals/conftest.py removed. New: regression/t4/__init__.py, regression/t4/__main__.py (calls report.main), regression/t5/__init__.py, regression/fixtures/conftest.py (`collect_ignore_glob = ["workspaces/*"]`). Imports rewritten to `regression.t4.*`, `sys.path` hacks and `__main__` runners removed. `EVAL_ROOT` is regression/t4; new `FIXTURES_ROOT` is regression/fixtures/workspaces, so `workspace_fixture` is now `calc_repo` and overlays are `overlays/bug_discount` (T2 and T4 YAML, test_report.py, test_coding_hard_oracle.py, test_assertions.py). regression/drivers/support/paths.py and bundles/workspace.py no longer search evals/. scripts/ci-local.sh and .github/workflows/ci.yml lose the four `evals/test_*.py` lines (the ci.yml "Evals self-checks" step is gone). test_judge.py is `@pytest.mark.live`. Outside the Creates list, each needed by the move: regression/conftest.py (`live`-marked items are deselected without `--live`, like t4/t5), pyproject.toml (ruff per-file-ignores `regression/fixtures/workspaces/**` = ALL, because the fixture repos now sit under the lint path and carry 6 deliberate findings; one comment path), .gitignore (build/ negations point at the new fixture paths; `/regression/t4/runs/` ignored), regression/t4/assertions.py (see Open questions). No src change.
Verify:    uv run pytest regression/selftest -q → 286 passed, 1 deselected (test_judge; it collects only with --live). uv run python -m regression.t4 --help → usage printed. uv run pytest tests regression -m "t1 or t2 or t3" --co -qq | rg -c 'workspaces/' → no matches (rg prints nothing, exit 1). bash scripts/ci-local.sh → exit 0 (4152 passed, 54 skipped, 1 xfailed; cli 357 passed; browser-mcp 320 passed, 18 skipped). uv run ruff format --check . → 821 files already formatted. uv run ruff check testkit tests regression → 33 findings, the same known ones. uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4152 passed, 54 skipped, 1 xfailed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run python -m testkit.failures latest → failures=0. rg -n 'fixtures/coding' regression evals tests → matches only in regression/plan/ (plan text, left as is); none in code, YAML or tests.
Catalog:   none filled. No T4 row has a case ID or Test cell until P4-03.
Recorded failures not fixed: none.
Open questions: (1) evals/assertions.py was moved to regression/t4/assertions.py, not deleted. The live runner (report.py) and the moved selftests call `evaluate_assertions(Scenario, EvalRun)`, which is a different API from regression/schema/assertions.py (`evaluate(dict, RunView)`). Merging them is not in P4-01's list; P4-03 (unified schema, `scenario_loader` as a wrapper) is the natural place. (2) docs/live-evals.md, docs/features.md and docs/verifier-agent-design.md still name `python -m evals.report`, `evals/suites/...` and `fixtures/coding/`. Not in P4-01's list. (3) `scenario_loader.agent_workspace_root()` still defaults to evals/smoke_agent/workspace, which P4-02 moves. (4) The T4 scenario YAML comments still name evals/smoke_agent paths and the evals/ copy of the baseline notes (smoke.md); P4-02 and P4-03 update them. (5) `ci-local.sh` still syncs `--extra evals`; only the live test_judge needs it now. (6) `evals/runs/` keeps its old run JSONs; new runs write to regression/t4/runs/.
Next:      P4-02

### 2026-10-26 P4-02 cursor
Task:      P4-02 Agent project, config split, judge config, preflight, ledger records
Status:    done. Plan deviation, user decision 2026-10-26: live secrets come from the repo-root .env, not regression/t4/agent/.env. The key name is matched case-insensitively (the root .env holds `ollama_api_key`); a process `OLLAMA_API_KEY` wins over the file. The missing-key message is `OLLAMA_API_KEY is not set (set it in the environment or in the repo-root .env)` instead of the §5.11 text. tasks.md, phase and reference files not edited.
Changed:   Moves (on disk, unstaged like P4-01's): evals/smoke_agent to regression/t4/agent; fixture_mcp_server.py to regression/fixtures/mcp/. monkeybot.verifier-on.yaml split into monkeybot.verifier-tracker.yaml (judge off, max_severity none) and monkeybot.verifier-judge.yaml (judge on, glm-5.3-flash, max_severity nudge); new mcp.progressive.json; mcp.json points at `${MONKEYBOT_AGENT_ROOT}/../../fixtures/mcp/fixture_mcp_server.py`; agent .env.example removed (it named the agent project as the key source). The fixture path was updated in tests/unit/core/mcp/test_mcp_client.py, regression/fixtures/configs/mcp.{autoconnect,broken,progressive,two_prefix}.json, regression/t2/loop/scenarios/mcp/catalog_diff_reload.yaml, and regression/drivers/support/tokens.py (docstring). New regression/t4/preflight.py: reads the key from the environment, then the repo-root .env; exits 2 and writes one run_error ledger record when it is missing; makes one cheap `stream` call (60 s timeout) whose error text is scrubbed. regression/t4/eval_config.yaml: judge_provider ollama-cloud, judge_model glm-5.3-flash. regression/t4/report.py runs the preflight before a live suite, writes one LedgerWriter failure per failed or errored scenario (tier t4, case, live block, artifacts = run JSON path; the run JSON is saved after each scenario), and adds --ledger-dir/--no-ledger. T4-GW-01: runner.py counts TurnComplete per request (0.3 s grace for a duplicate); assertions.live_invariant_failures checks every live scenario; models/persistence/scenario_loader carry turn_completes and `case`; agent_workspace_root defaults to regression/t4/agent/workspace. New selftests regression/selftest/test_preflight.py, test_agent_project.py, test_live_ledger.py with recorded/t4_run_smoke_sample.json (a run JSON written in the run-file shape; no live run produced it). .gitignore adds /regression/t4/agent/{.env,data/,workspace/}. Comments in four T4 scenarios no longer name evals/smoke_agent. No src change.
Verify:    Offline form of `env -u OLLAMA_API_KEY uv run python -m regression.t4 --suite smoke` (report.main with preflight.ENV_FILE pointed at a missing file, both key spellings unset, network sandboxed) → exit 2, the message above, run_error recorded to test-runs/20261026T221441Z/20261026T221441Z-8248de8-13135.jsonl (run_start, run_error reason missing_key, run_end errors=1). The literal command was not run: it now finds the key in the repo-root .env and would make a live stream call. uv run pytest tests/unit/core/mcp/test_mcp_client.py regression/selftest -q → 382 passed, 1 deselected. uv run python -m regression.t4 --help → usage. uv run ruff format --check . → 825 files already formatted. uv run ruff check testkit tests regression → 33 known findings in 13 files (the test_mcp_client.py five are the same at HEAD). uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4192 passed, 54 skipped, 1 xfailed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run python -m testkit.failures latest → failures=0. ci-local.sh not run (no gate script changed in P4-02).
Catalog:   none filled. T4-GW-01's Test cell must be a t4-tier path, and the selftest lives in regression/selftest; the cell waits for P4-03.
Recorded failures not fixed: none. test-runs/20261026T221441Z/...-13135.jsonl is the intended run_error from the verify check above.
Open questions: (1) The preflight-only live run in Done-when was not run (no live confirmation this session). It is the one unverified check. (2) The agent gateway itself reads only its process env and `<agent root>/.env` (core/layout.py), with the exact name OLLAMA_API_KEY. The launcher in P4-05 must start it with OLLAMA_API_KEY exported (preflight.ensure_key exports it into its own process, so a child started from there inherits it). (3) deepeval's pytest plugin (`deepeval.plugins.plugin` entry point) loads `<cwd>/.env` into os.environ at startup unless DEEPEVAL_DISABLE_DOTENV=1, so every pytest session run from the repo root carries the real `ollama_api_key`. test_preflight removes every spelling of the name before each test. testkit/scrub.py matches `_API_KEY` case-sensitively, so it would not redact a value held only under the lowercase name (a live run also exports the uppercase name, which is redacted). Options: set DEEPEVAL_DISABLE_DOTENV=1 for test runs (pytest config or gate scripts), and/or make the scrubber's suffix match case-insensitive. Both are outside P4-02's list. (4) The working tree's moves are unstaged (git status shows deletions plus untracked files, P4-01's too). When staging, regression/t4/agent/workspace/.gitkeep and workspace/skills/browser/playbooks/.gitkeep need `git add -f` (the workspace/ ignore entry covers them). (5) .github/workflows/live-eval-smoke.yml, docs/live-evals.md, getting-started.md, CONTRIBUTING.md and README.md still name evals/smoke_agent and its .env; P4-05 owns the workflow and docs.
Next:      P4-03

### 2026-10-26 P4-02 follow-up cursor
Task:      P4-02 close-out: live preflight and open question (3)
Status:    done
Changed:   No src change. pytest.ini addopts adds `-p no:deepeval`. Root conftest.py sets DEEPEVAL_DISABLE_DOTENV=1 (setdefault), so a test session no longer gets the root .env key. testkit/scrub.py matches the *_API_KEY / *_TOKEN / *_SECRET suffixes case-insensitively, and tests/unit/testkit/test_scrub.py adds test_redacts_lowercase_secret_env_names. User decisions 2026-10-26.
Verify:    With the user's confirmation, ran preflight.run_preflight(suite="smoke") against ollama-cloud/glm-5.3-flash: passed (one stream call, no T4 suite). uv run pytest tests/unit/testkit/test_scrub.py regression/selftest -q → 331 passed, 1 deselected. Probe test under pytest: neither ollama_api_key nor OLLAMA_API_KEY is in os.environ.
Catalog:   n/a
Recorded failures not fixed: none
Open questions: none; P4-02 open questions (1) and (3) are resolved.
Next:      P4-03

### 2026-10-26 P4-03 cursor
Task:      P4-03 Convert the 29 live scenarios and the suites
Status:    done
Changed:   Moves used plain `mv`, not `git mv`: regression/t4/ is untracked (P4-01/P4-02 left their moves unstaged). tools/core_read{,_error}.yaml and tools/core_write.yaml went to file/, and tools/core_run_command{,_sandbox}.yaml went to shell/. All 29 YAMLs now have `case:` (IDs in suite order) and `mode: live`. Legacy keys became their §5.3.1 forms: max/min_tool_calls → `tool_calls: {max|min}`, min_subagent_calls → `tool_calls: {task: {min}}`, max/min_verdicts and verdict_status_in → `verdicts: {...}`. T4-CTX-01 is retuned to `config: model.context_window: 6000` with `min_summarizations: 1`. T4-SKILL-01 has `required_tools: [list_skills, read_file]`. smoke.yaml uses the new paths and adds file/core_read_error. scenario_loader.py is a wrapper over regression.schema (`load_scenario_file`; `load_scenarios_by_id` loads only the named files). regression/plugin.py collects regression/t4/scenarios/**/*.yaml as t4 items: `--suite` filters by regression/t4/suites/<name>.yaml, and the item runs preflight once then report.run_scenario against `--agent-url`, then AGENT_URL, then http://127.0.0.1:8080. Collection reads only `case` and `tags`. Outside the Creates list: regression/schema/assertions.py registers `min_summarizations` (live only; the T4-CTX-01 row names it). regression/t4/assertions.py evaluates `tool_calls` and `verdicts` and fails on keys it does not evaluate. report.py renames `_run_scenario` to `run_scenario` and adds DEFAULT_AGENT_URL. New regression/t4/conftest.py ignores agent/*. regression/selftest/test_report.py adds `case`/`mode` to three inline snippets. New regression/selftest/test_live_scenarios.py. tests/contracts/test_catalog_pairing.py seed counts went from 366/375 to 381/405 (filled rows/tests). No src change.
Verify:    uv run pytest regression/t4 --live --collect-only -qq | wc -l → 31: 29 node ids plus pytest's blank line and "29 tests collected" footer (`rg -c '::'` → 29). --suite smoke → 12 items; without --live → 29 deselected. Collection with socket connect, subprocess.Popen and preflight patched to raise → 29 collected, no calls, runner/report not imported. All 29 load with EVAL_VERIFIER_MODE=on and =off. uv run pytest regression/selftest -q → 334 passed, 1 deselected. uv run ruff format --check . → 827 files already formatted. uv run ruff check testkit tests regression → the known 33 findings; changed files clean. uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4201 passed, 54 skipped, 1 xfailed. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run python -m testkit.failures latest → failures=0. ci-local.sh not run (no gate script changed). No live run.
Catalog:   15 rows filled: T4-FILE-01..03, T4-SHELL-01..02, T4-CODE-01..03, T4-CODE-04..10, T4-CTX-01, T4-VER-01..05, T4-SUB-01, T4-SKILL-01, T4-MCP-01..03, T4-MEM-01..02, T4-TODO-01 (scenario paths) and T4-GW-01 (regression/t4/assertions.py, home of live_invariant_failures).
Recorded failures not fixed: test-runs/20261026T230311Z/20261026T230311Z-8248de8-16856.jsonl: tests/contracts/test_catalog_pairing.py::test_catalog_pairing, `assert 381 == 366`, the seed count these filled rows change; counts updated as allowed.
Open questions: (1) The live runner does not apply a scenario's `config`, so T4-CTX-01 needs the gateway started with model.context_window 6000. The value is unvalidated until a live run. (2) Pytest t4 items write ledger records without the `live` block or artifacts and do not save the run JSON; report.py (`python -m regression.t4`) still does. (3) The "Plus" checks in the T4-FILE-01/03, SHELL-01, MEM-01..02 and TODO-01 rows, and `max_tool_errors: 0` in T4-FILE-02's row, were not added: they change expectations and are not in P4-03's list. (4) The two assertion engines remain separate. The live engine accepts the unified spellings plus legacy keys. (5) regression/selftest/recorded/t4_run_smoke_sample.json still names tools/core_read (recorded data, not changed).
Next:      P4-04

### 2026-10-26 P4-05 cursor
Task:      P4-05 Live workflow and artifacts
Status:    done
Changed:   .github/workflows/live-eval-smoke.yml only. Triggers kept (pull_request, push to main, workflow_dispatch) and the gate job's decide logic kept. Removed the `secrets` job (the NVIDIA_API_KEY gate) and every provider-key env (NVIDIA_API_KEY on the gateway boot and report steps); `live-eval` now needs only `gate`. working-directory and the lockfile trigger path: evals/smoke_agent → regression/t4/agent. MONKEYBOT_AGENT_CONFIG: regression/t4/agent/monkeybot_config/monkeybot.yaml. The other old paths moved too, so the workflow names only new ones: `python -m evals.report` → `python -m regression.t4`, `--baseline evals/baselines/smoke.json` → regression/t4/baselines/smoke.json, artifact `evals/runs/` → regression/t4/runs/ (persistence.save_run creates it), `evals.diff` → `regression.t4.diff`. Also removed `JUDGE_PROVIDER: nvidia` / `JUDGE_MODEL: nvidia/nemotron-3-ultra-550b-a55b`: they override eval_config.yaml and would need an NVIDIA key, so the judge now comes from eval_config.yaml (ollama-cloud / glm-5.3-flash, D12). The header comment states D1 (no key in GitHub; the smoke suite runs locally), and the comments no longer name NVIDIA, inkling or smoke_agent (12 smoke scenarios, 11 delays). No key added, no live-suite step added, no src change, no live run.
Verify:    uvx --from actionlint-py actionlint .github/workflows/live-eval-smoke.yml → exit 0, no findings (actionlint 1.7.12; shellcheck is not installed, so the shell in `run:` blocks was not linted; the PyPI name is actionlint-py, `uvx actionlint` does not resolve). rg -n 'evals/|OLLAMA_API_KEY|NVIDIA_API_KEY|secrets\.' .github/workflows/live-eval-smoke.yml → no matches (exit 1). Every referenced path exists in the working tree: uv.lock, cli/uv.lock, regression/t4/agent/{uv.lock,monkeybot_config/monkeybot.yaml}, regression/t4/{report.py,diff.py,eval_config.yaml,suites/smoke.yaml,baselines/smoke.json}, docs/live-evals.md, src/monkeybot/gateway/main.py; regression/t4/runs/ is created at run time. uv run ruff format --check . → 827 files already formatted.
Catalog:   n/a (no case added)
Recorded failures not fixed: none
Open questions: (1) The kept triggers still start the job on develop→main PRs, lockfile PRs, pushes to main, and manual runs. With no key, the gateway boots and `python -m regression.t4` exits 2 at preflight. But the step pipes into `tee` under GitHub's default `bash -e` (no pipefail), so the step and job show green with an empty scorecard. This was already true of `--fail-on-regression` before P4-05. Options: add `shell: bash` (which runs `-eo pipefail`) or `set -o pipefail` to the report step so the missing key fails visibly; reduce the triggers to workflow_dispatch (09-ci lists no GitHub job for live-smoke); or leave it. Each is outside P4-05's listed changes. (2) docs/live-evals.md and the other docs that still name evals/smoke_agent were not touched (not in P4-05's list).
Next:      P4-06 after P4-04 is done (needs the user's confirmation of the local live smoke run). P4-04 was in progress when P4-05 finished, so Next task is left for the P4-04 agent.

### 2026-10-26 P4-04 cursor
Task:      P4-04 New T4 cases and suites
Status:    blocked (partial). Seven of the eleven `+` scenarios cannot be expressed with today's live schema, runner and assertion engine, and extending them is not in P4-04's list; they wait for the user's answer to open question (1). Live YAML allows no `steps:`, the runner only sends messages and reads tool names and args, assistant text, summarizations, verdicts and TurnComplete, it ignores `config:`, and the live engine has no argument, Evidence-path, subagent-manifest, prompt or SSE-reconnect matcher. The other six cases are written. Blocked: T4-HOOK-01 hooks/evidence_citation_live (needs a code grader: every `Evidence:` path exists and was read). T4-SUB-02 subagents/persona_dispatch (needs a child-manifest `subagent_type` check, and the agent project defines no persona to dispatch to). T4-SCHED-01 scheduler/http_one_tick_live (needs HTTP steps: POST /loops, POST /invoke-tick, then a history read). T4-SCHED-02 scheduler/model_starts_loop (the stop guard is `max_ticks`/`max_runtime` in the start_loop args; `unbounded: true` also passes validation, so required_tools plus max_tool_errors cannot tell). T4-MEM-03 memory/search_past_session (needs "session 2 ran `run_command mempalace search`"; the live tool_calls count names only). T4-CFG-01 config/reload_memory_off_live (needs POST /config/reload and a look at the next prompt's wake-up block). T4-GW-02 gateway/live_resume (needs a client that drops mid-delta and reconnects with Last-Event-ID). regression/t4/suites/scheduler.yaml is not created: both SCHED cases are blocked, and test_suites_name_existing_scenarios requires a non-empty suite naming existing files.
Changed:   New scenarios: regression/t4/scenarios/file/grep_absence_honesty.yaml (T4-FILE-04; workspace att_ws, whose binary PNGs make grep return incomplete_scan; response_not_contains plus judge metrics; required_tools [grep] so the scan ran), context/post_compaction_continuity.yaml (T4-CTX-02; calc_repo_green, four turns, `config: model.context_window: 6000` as T4-CTX-01; min_summarizations 1 plus workspace grader: file contents, an import check and pytest), attachments/describe_image_live.yaml (T4-ATT-01, tag `optional`; att_ws/tiny.png is 1x1 pure red; required_tools [load_file], final_text_regex `\bred\b`), mcp/enable_then_echo_live.yaml (T4-MCP-04; `config.env.MCP_CONFIG: monkeybot_config/mcp.progressive.json`; required_tools [enable_mcp, fixture__echo], max_tool_errors 0). New suites: regression/t4/suites/{context,memory,mcp,subagents,nightly}.yaml (memory and subagents hold only the existing cases until MEM-03 and SUB-02 exist; nightly is the union of every other suite in suite order). New Python: regression/t4/python/__init__.py (empty, as regression/t2/python), test_provider_live_matrix.py (T4-PROV-01; module mark `live`, items also get `t4` from the folder; Ollama Cloud always and fails without the key; 10 other providers skip with the reason unless their key is set), test_docker_image_health.py (T4-OPS-01; `live`; builds docker/Dockerfile unless MONKEYBOT_DOCKER_IMAGE is set, runs it with the compose demo overlay (fake provider), polls /health on container port 8080, skips when the docker CLI is missing). Outside the Creates list: docs/testing.md (six Test cells), tests/contracts/test_catalog_pairing.py (seed counts 381/405 → 387/411, filled rows/tests), regression/selftest/test_live_scenarios.py (four EXPECTED_CASES entries; the docstring no longer says 29). No src change, no live run, no docker build.
Verify:    uv run pytest regression/t4 --live --collect-only -qq → 45 tests collected (`rg -c '::'` → 45: 33 scenarios plus 12 Python items, 11 provider params and the docker test); without --live → 45 deselected. --suite context/memory/mcp/subagents/nightly/smoke → 14/14/16/13/42/24 (each is the suite's scenarios plus the 12 Python items; see open question (4)). Collection with socket.connect, socket.create_connection, subprocess.Popen and preflight.run_preflight{,_once} patched to raise → 45 items, no calls, runner/report not imported. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. T4 rows without a Test cell (catalog parser) → T4-HOOK-01, T4-SUB-02, T4-SCHED-01, T4-SCHED-02, T4-MEM-03, T4-CFG-01, T4-GW-02, T4-RT-01, so the "only T4-RT-01" check does not hold yet. uv run pytest regression/selftest -q → 334 passed, 1 deselected. uv run ruff format --check . → 830 files already formatted. uv run ruff check testkit tests regression → 33 findings in the 13 known files; the changed files are clean. uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4201 passed, 54 skipped, 1 xfailed. uv run python -m testkit.failures latest → failures=0.
Catalog:   6 rows filled: T4-FILE-04, T4-CTX-02, T4-ATT-01, T4-MCP-04 (scenario paths), T4-PROV-01, T4-OPS-01 (regression/t4/python paths). Left empty: the seven blocked rows and T4-RT-01 (deferred).
Recorded failures not fixed: test-runs/20261027T002217Z/20261027T002217Z-8248de8-23115.jsonl: tests/contracts/test_catalog_pairing.py::test_catalog_pairing, `assert 387 == 381`, the seed count these six rows change; counts updated as allowed.
Open questions: (1) How should the seven blocked cases be written? Options: (a) extend the live tier in a follow-up task: `steps:` for live mode (HTTP calls, reload, reconnect), live matchers for tool arguments per session, Evidence paths, the child manifest, plus `tool_error_codes`, then write the YAML files; (b) write them as `regression/t4/python/` tests that drive the gateway directly, which changes §4.4's file layout; (c) defer some (for example T4-CFG-01 and T4-GW-02). T4-SUB-02 also needs a persona in the agent project's monkeybot.yaml. (2) The live runner does not apply `config:` (P4-03 open question 1). The context suite needs a gateway with model.context_window 6000; mcp/enable_then_echo_live needs MCP_CONFIG=monkeybot_config/mcp.progressive.json while T4-MCP-01..03 need the autoConnect mcp.json, so no single gateway runs the whole mcp suite green; nightly inherits every suite's needs (drift needs EVAL_VERIFIER_MODE). Options: the runner applies `config.env` per scenario (a restart per config), or split suites by gateway config. (3) T4-FILE-04 and T4-ATT-01 are in no suite. Nightly is the literal union of the other suites, and FILE-04's row does not say to add it to smoke. Should they go into nightly? (4) `--suite` filters only scenario YAML, so `pytest regression/t4 --live --suite <name>` also runs the provider matrix and the docker build. That fits T4-PROV-01 ("runs first in every live suite") but not the docker test. `python -m regression.t4 --suite` runs only scenarios. (5) T4-PROV-01 needs `MONKEYBOT_LIVE_MATRIX_MODEL_<ID>` for any non-Ollama provider, because providers have no default model id; with the key set but no model, it skips with that reason. Vertex and Bedrock count GOOGLE_APPLICATION_CREDENTIALS / AWS_ACCESS_KEY_ID as their key. (6) Not checked live: whether glm-5.3-flash accepts images (ATT-01 is optional), whether the att_ws grep returns incomplete_scan on the live agent (expected from workspace_service, since its PNGs are binary), and whether 6000 tokens leaves room for the agent's base prompt.
Next:      P4-04 again after the user answers (1). P4-06 waits on P4-04 being done and then on the user's live-run confirmation.

### 2026-10-26 P4-04/P4-05 user decisions cursor
Task:      Decisions on P4-04 open questions (1) and (2), P4-05 open question (1), and live runs
Status:    recorded
Changed:   .github/workflows/live-eval-smoke.yml triggers cut to workflow_dispatch only, header comment updated (user decision: live runs are local per D1). The gate job's PR logic and the PR-comment step are now unreachable but left in place.
Verify:    uvx --from actionlint-py actionlint .github/workflows/live-eval-smoke.yml → exit 0; rg -n 'evals/|OLLAMA_API_KEY|NVIDIA_API_KEY|secrets\.' on it → no matches
Catalog:   n/a
Recorded failures not fixed: none
Open questions: none. User decisions 2026-10-26: (a) the 7 blocked P4-04 cases (T4-HOOK-01, T4-SUB-02, T4-SCHED-01, T4-SCHED-02, T4-MEM-03, T4-CFG-01, T4-GW-02) are built by extending the live tier (live steps:, argument and per-session matchers, Evidence/manifest/SSE checks), in scope for P4-04; (b) the live runner applies a scenario's config: by starting or reconfiguring the gateway per scenario, in scope for P4-04; (c) live runs approved: P4-06 local smoke only. P3-02 and P3-03 are not yet approved.
Next:      P4-04 (resumed), then P4-06

### 2026-10-26 P4-04 cursor
Task:      P4-04 New T4 cases and suites
Status:    done
Changed:   Seven scenarios: hooks/evidence_citation_live.yaml (T4-HOOK-01), subagents/persona_dispatch.yaml (T4-SUB-02), scheduler/http_one_tick_live.yaml (T4-SCHED-01), scheduler/model_starts_loop.yaml (T4-SCHED-02), memory/search_past_session.yaml (T4-MEM-03), config/reload_memory_off_live.yaml (T4-CFG-01), gateway/live_resume.yaml (T4-GW-02). Test-only persona: regression/t4/agent/monkeybot_config/AGENT.researcher.md, overlaid on the SUB-02 scenario config (default monkeybot.yaml unchanged). New suite regression/t4/suites/scheduler.yaml. Nightly adds T4-FILE-04, T4-ATT-01, and the seven. Every suite names test_provider_live_matrix.py; test_docker_image_health.py is unnamed, so T4-OPS-01 runs only with no `--suite`. Live engine: tool_args (stop_guard, command_contains), per_session, evidence_paths, subagent_manifest, system_prompt, chat_history, sse.deltas_equal_final, plus setup steps create_loop / invoke_tick / read_history and when-steps reload_config / confirm_tool / disconnect_sse / reconnect_sse. Runner stamps a missing request_id, reads history and transcripts, and run_scenario accepts a plain turn list. report.run_suite and the pytest plugin group by GatewaySpec and restart or reload between groups; collection does not start a gateway. No src/monkeybot change, no live run.
Verify:    uv run pytest regression/t4 --live --collect-only -qq → 52 `::` node ids (11 provider params, docker, 40 scenarios). --suite context → 13 (provider + 2 scenarios, no docker). --suite nightly → 50 (provider + 39 scenarios, no docker). Collection with subprocess.Popen, socket.create_connection, and preflight patched to raise → 52 collected; the ledger's git call hits Popen via subprocess.run, and pytest-rerunfailures opens socket.socket. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed (seeds 394 filled / 418 tests). T4-RT-01 stays empty. uv run pytest regression/selftest -q → 364 passed, 1 deselected. uv run ruff format --check . → 832 files already formatted. uv run ruff check testkit tests regression → 33 findings in the 13 known files; none added. uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto → 4231 passed, 54 skipped, 1 xfailed. uv run python -m testkit.failures latest → failures=0 (test-runs/20261027T013601Z/20261027T013601Z-8248de8-30657.jsonl).
Catalog:   7 rows filled: T4-HOOK-01, T4-SUB-02, T4-SCHED-01, T4-SCHED-02, T4-MEM-03, T4-CFG-01, T4-GW-02. T4-RT-01 left empty (deferred). Pairing seeds 387/411 → 394/418.
Recorded failures not fixed: none. Two earlier runs from this task were fixed before the green verify: test-runs/20261027T013136Z/20261027T013136Z-8248de8-30246.jsonl (pairing, Test cells had backticks so the paths did not exist) and test-runs/20261027T013258Z/20261027T013258Z-8248de8-30375.jsonl (test_live_ledger, run_scenario assumed ScenarioRun; a plain list is now wrapped).
Open questions: none. The seven cases are not executed live here; P4-06 is the approved local smoke run.
Next:      P4-06

### 2026-10-26 P4-06 cursor
Task:      P4-06 Live smoke; phase exit
Status:    done
Changed:   regression/plan/status.md only. No src/monkeybot change, no scenario or baseline edit, no cassette. Gateway started from regression/t4/agent on 127.0.0.1:8787 (PORT=8787, SANDBOX_ENABLED=false) and stopped afterwards. A temporary proof scenario and suite were created for one run and deleted.
Verify:    EVAL_SCENARIO_DELAY_SEC=150 uv run python -m regression.t4 --suite smoke --agent-url http://127.0.0.1:8787 --baseline regression/t4/baselines/smoke.json. Judge stayed eval_config.yaml (ollama-cloud/glm-5.3-flash). Exit 0. 12/12 scenarios passed. Scorecard verdict FAIL is baseline comparison only: output tokens 14426 -> 17739 (> 20%), p95 latency 30131ms -> 311146ms (> 20%), tool errors 1 -> 4. Run JSON regression/t4/runs/e16c4a2f-6c65-4ac4-b43f-4b00e813cf0d.json. Smoke ledger test-runs/20261027T014504Z/20261027T014504Z-8248de8-32202.jsonl failures=0, live provider ollama-cloud model glm-5.3-flash. Proof (deleted afterwards): copy of file/core_read with response_contains of a token that cannot appear; ledger test-runs/20261027T024222Z/20261027T024222Z-8248de8-33892.jsonl case P4-06-LEDGER-PROOF, provider ollama-cloud, model glm-5.3-flash, artifacts regression/t4/runs/4c8b84ca-f650-45d9-952c-5863c1928c25.json. T4 catalog Test cells: every row filled except T4-RT-01.
Catalog:   n/a (confirmed; only T4-RT-01 is empty)
Recorded failures not fixed: none in the smoke suite. The proof failure is deliberate and the scenario was deleted: test-runs/20261027T024222Z/20261027T024222Z-8248de8-33892.jsonl P4-06-LEDGER-PROOF, response_contains token not in the reply.
Open questions: none. P3-02 and P3-03 still need user confirmation.
Next:      P3-02 and P3-03 still need the user's confirmation. Do not start them.

### 2026-10-27 P3-03 cursor
Task:      P3-03 Wire cassettes and the staleness check (T3-PROV-06 for Ollama, T3-PROV-07, T3-PROV-08)
Status:    done
Changed:   regression/t3/tools/record_wire.py and check_staleness.py; regression/t3/test_replay_wire.py; regression/t3/wire/ollama_cloud/glm_tool_turn.json (tool turn, add a=2 b=3) and thinking_only_completion.json (thinking then Done, no text; max_tokens 64 so the live stream stops during thinking, truncated true). scripts/ci-local.sh and .github/workflows/ci.yml cassette-staleness step (workflow stays workflow_dispatch; no provider key). Catalog Test cells for T3-PROV-06, T3-PROV-07, T3-PROV-08. Pairing seeds recounted to 397 filled rows and 421 test paths. No src/monkeybot change. No gateway.
Verify:    uv run pytest regression/t3/test_replay_wire.py -q → 3 passed in 0.33s (socket.connect refused). uv run python -m regression.t3.tools.check_staleness → ok: 2 wire cassettes match. Same command after setting glm_tool_turn.json sdk openai to 0.0.0-stale → exit 1; file restored to 2.36.0. uv run ruff format --check . → 835 files already formatted. uv run ruff check testkit tests regression → 33 findings, none in the new files. uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run python -m testkit.failures latest → failures=0 (test-runs/20261027T133822Z/20261027T133822Z-8248de8-39156.jsonl). Full bash scripts/ci-local.sh not run.
Catalog:   T3-PROV-06, T3-PROV-07, T3-PROV-08 → regression/t3/test_replay_wire.py. T3-PROV-01..05 left empty. T3-PROV-06 is one row; only the Ollama family is recorded.
Recorded failures not fixed: none
Open questions: none
Next:      P3-02 (in progress). Phase 3 stays in progress until P3-02 is done. P5-01 is also in progress.

### 2026-10-27 P5-01 cursor
Task:      P5-01 Statistics, comparison, and class lists
Status:    done
Changed:   Created regression/t5/__main__.py, regression/t5/stats.py, regression/t5/compare.py, regression/t5/suites/reliability.yaml, regression/t5/suites/capability.yaml, regression/t5/suites/drift.yaml, regression/selftest/test_trials_stats.py. Extended regression/t5/trials.py so a trial summary is scored and fail/warn verdicts go to the ledger; --update-baseline is the only writer of regression/t5/baselines/. Filled docs/testing.md §8 Test cells. Updated the pairing seed in tests/contracts/test_catalog_pairing.py (397 filled / 421 paths → 406 / 430) so the new cells match. No src/monkeybot change. No live trial. No baseline file written.
Verify:    uv run pytest regression/selftest/test_trials_stats.py -q → 12 passed in 0.31s. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed in 10.26s. An earlier run of the new test was fixed before that: test-runs/20261027T134744Z/20261027T134744Z-8248de8-40067.jsonl, regression/selftest/test_trials_stats.py, TypeError cannot unpack Interval (the test now reads .low and .high).
Catalog:   T5-SKILL-01, T5-TODO-01, T5-SUB-01 → regression/t5/suites/reliability.yaml. T5-CTX-01, T5-CODE-01, T5-SCHED-01, T5-MCP-01, T5-MEM-01 → regression/t5/suites/capability.yaml. T5-VER-01 → regression/t5/suites/drift.yaml. T5-GW-01 and T5-GW-02 left empty.
Recorded failures not fixed: none
Open questions: none
Next:      P5-02 (needs the user's confirmation for the live baseline run). P3-02 is still in progress.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live)
Status:    blocked (doom loop and truncation did not occur in three tries; compaction replay is cassette_stale; T3-VER-01 classifier and judge JSON are not cassette calls)
Changed:   regression/t3/trajectories/ctx/replay_live_compaction.{cassette.json,yaml} from context/summarization_trigger (session 20261027T132946Z_2ffe49a6-7481-409a-aeb2-97e36e09155f). regression/t3/trajectories/shell/replay_live_pytest_run.{cassette.json,yaml} from coding/fix_failing_test (session 20261027T140236Z_73d3e59c-93f9-4da4-8a6b-16900597c912); tool path and cwd values that started with the runner directory eval-coding-fix_failing_test were made fixture-relative so the cassette replays against calc_repo, and model.max_turns is 25 to match the live agent. docs/testing.md Test cells for T3-CTX-01 and T3-SHELL-01. Pairing seeds recounted to 408 filled rows and 432 test paths. No doom, truncation, or verifier cassette. No src/monkeybot change. Gateway on 8787 started for this task and stopped afterwards.
Verify:    uv run pytest regression/t3/trajectories/shell/replay_live_pytest_run.yaml -q three times → passed (digest warning only). uv run pytest regression/t3/test_replay_trajectories.py -q → 15 passed (socket.connect refused). uv run ruff format --check . → 839 files already formatted. uv run ruff check testkit tests regression → 33 findings in the same 13 files; none added. uv run mypy src/monkeybot testkit → no issues in 287 files. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run python -m testkit.failures latest → failures=0 (test-runs/20261027T142528Z/20261027T142528Z-8248de8-44533.jsonl).
Catalog:   T3-SHELL-01 → regression/t3/trajectories/shell/replay_live_pytest_run.yaml. T3-CTX-01 → regression/t3/trajectories/ctx/replay_live_compaction.yaml (replay is not green). T3-LOOP-01, T3-LOOP-02, and T3-VER-01 left empty.
Recorded failures not fixed: test-runs/20261027T141509Z/20261027T141509Z-8248de8-43284.jsonl T3-CTX-01 regression/t3/trajectories/ctx/replay_live_compaction.yaml cassette_stale: tool read_file changed. The same run's T3-SHELL-01 strict_script failure was the loop driver's default max_turns 10 against 13 recorded calls; the scenario now sets model.max_turns 25 and the later replays passed.
Open questions: T3-VER-01. The live drift/declares_done run wrote a VerifierVerdict (status on_track, triggering_signals a one-item list, judge_tokens an integer) and six ordinary ProviderResponses. Classifier JSON and judge JSON are not provider calls, so they do not fit the §5.5.1 calls list. No verifier cassette was written. Compaction did occur (first successful summary after provider response index 4, turns_summarized 3) but the summarizer's provider.stream is not in the transcript; replay treats that stream as the next cassette call and the empty tool list fails staleness. Doom loop and finish=length did not occur. Please say whether to insert recorded summarizer calls, skip staleness on empty-tool streams, or leave T3-CTX-01 blocked.
Next:      P5-02 stays next. Phase 3 is not done. P3-03 is already done, not in parallel.

### 2026-10-27 P3-02 user decision cursor
Task:      P3-02 Record the T3a cassettes (live)
Status:    blocked
Changed:   regression/plan/status.md only. No new live run, no src change, no cassette edit.
Verify:    n/a
Catalog:   unchanged. T3-SHELL-01 is filled and replays. T3-CTX-01 is filled but not green. T3-LOOP-01, T3-LOOP-02, and T3-VER-01 stay empty.
Recorded failures not fixed: T3-CTX-01 cassette_stale: tool read_file changed (test-runs/20261027T141509Z/20261027T141509Z-8248de8-43284.jsonl)
Open questions: none. User 2026-10-27: leave T3-CTX-01 blocked; do not retry the doom loop or truncation; leave T3-VER-01 blocked; do not start the P5-02 live baselines.
Next:      P5-02 when the user confirms the live baselines. Phase 3 stays in progress.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live)
Status:    in-progress (T3-LOOP-01 replays; T3-CTX-01, T3-LOOP-02, and T3-VER-01 stay blocked)
Changed:   regression/t3/trajectories/loop/replay_live_doom_loop.{cassette.json,yaml} from a live turn that asked for three identical read_file calls on skills/browser/SKILL.md (session 20261027T144852Z_c83eeb0b-3417-4645-8b6a-ae5ef1806a25, gateway on 8808, temporary agent copy). docs/testing.md Test cell for T3-LOOP-01. Pairing seeds 409 filled rows and 433 test paths. No src/monkeybot change. Gateway on 8808 stopped afterwards. The 8797 smoke gateway was left running.
Verify:    uv run pytest regression/t3/trajectories/loop/replay_live_doom_loop.yaml -q → 1 passed, digest warnings only (tool_schema_digest differs; advertised tools no longer present: disable_mcp, enable_mcp, load_file, todo_list, web_search). uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed.
Catalog:   T3-LOOP-01 → regression/t3/trajectories/loop/replay_live_doom_loop.yaml. T3-SHELL-01 still replays. T3-CTX-01 still not green. T3-LOOP-02 and T3-VER-01 stay empty.
Recorded failures not fixed: none for T3-LOOP-01. The live runner raised RuntimeError on the doom-loop Error event; the transcript still held the three reads, the intervention, and the recovery reply, and the offline replay passed.
Open questions: none
Next:      P3-02 remains open for T3-CTX-01, T3-LOOP-02, and T3-VER-01.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-LOOP-02 only
Status:    blocked (forced length cap never produced a truncated tool batch)
Changed:   regression/plan/status.md only. No cassette, no src/monkeybot change. Temporary gateway on 8803 with model.max_tokens 160, 2000, 4500, and 900, thinking off, then stopped.
Verify:    live turns against ollama-cloud/glm-5.3-flash. Each finish was length and output_tokens equaled the cap. Transcripts had tool_requests [] and no truncated_batch. A direct provider probe with think false and tool_choice write_file did the same: the cap landed in reasoning or plain text.
Catalog:   T3-LOOP-02 stays empty. T3-LOOP-01 and T3-SHELL-01 unchanged.
Recorded failures not fixed: none. No replay was run because there was no cassette.
Open questions: none. The scripted rejection remains T2-LOOP-04.
Next:      P3-02 stays open for T3-CTX-01 and T3-VER-01.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-LOOP-02 prompt retry
Status:    blocked (no-think prompt still produced a text-only length stop)
Changed:   no cassette and no src/monkeybot change. The live user message told the model not to think, plan, or explain, and to execute write_file only. Gateway on 8803, max_tokens 500, thinking off, then stopped.
Verify:    session 20261027T150257Z_t3loop02aac1490a. finish length, output_tokens 500, tool_requests empty, no truncated_batch. The reply started "The user wants me to write..."
Catalog:   T3-LOOP-02 stays empty.
Recorded failures not fixed: none
Open questions: none
Next:      P3-02 stays open for T3-CTX-01 and T3-VER-01.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-LOOP-02 cap 10000
Status:    blocked (10000-token cap still produced a text-only length stop)
Changed:   no cassette and no src/monkeybot change. Same no-think write_file prompt. Gateway on 8803, max_tokens 10000, thinking off, then stopped.
Verify:    session 20261027T150516Z_t3loop02de3c2c52. finish length, output_tokens 10000, tool_requests empty, no truncated_batch. The reply ended as the model was about to emit the tool call.
Catalog:   T3-LOOP-02 stays empty.
Recorded failures not fixed: none
Open questions: none
Next:      P3-02 stays open for T3-CTX-01 and T3-VER-01.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-VER-01
Status:    in-progress (T3-VER-01 replays; T3-CTX-01 and T3-LOOP-02 stay blocked)
Changed:   Cassette gains classifier and judge lists. from_transcript turns each VerifierVerdict into a judge completion and reads classifier rows from the session SQLite goal ledger. Replay feeds those lists to ScriptedClassifier and ScriptedJudge. regression/t3/trajectories/verifier/replay_drift_tape.{cassette.json,yaml} from drift/declares_done session 20261027T140423Z_a568bf19-b738-48e6-b1cd-5b92eb155736 and gateway db data/gateways/0be8640d2635/monkeybot.db. docs/testing.md Test cell. Pairing seeds 410 filled rows and 434 test paths. Cassette shape noted in regression/plan/reference/05.5-cassettes.md. No src/monkeybot change. No new live run.
Verify:    uv run pytest regression/selftest/test_from_transcript.py regression/t3/test_replay_trajectories.py tests/contracts/test_catalog_pairing.py regression/t3/trajectories/verifier/replay_drift_tape.yaml -q --count=3 → 103 passed, 6 warnings in 41.04s. The drift tape asserts one on_track verdict with constraint_touch and no harness interventions.
Catalog:   T3-VER-01 → regression/t3/trajectories/verifier/replay_drift_tape.yaml. T3-LOOP-01 and T3-SHELL-01 still replay. T3-CTX-01 still not green. T3-LOOP-02 stays empty.
Recorded failures not fixed: T3-CTX-01 cassette_stale: tool read_file changed (test-runs/20261027T141509Z/20261027T141509Z-8248de8-43284.jsonl). T3-LOOP-02 has no cassette.
Open questions: none
Next:      P3-02 stays open for T3-CTX-01 and T3-LOOP-02. P5-02 live baselines still wait on the user.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-LOOP-02 no custom cap
Status:    blocked (thinking-only replies hit the client default of 60000 and never called a tool)
Changed:   no cassette and no src/monkeybot change. thinking_budget -1, reasoning_effort omitted, max_tokens omitted from the temporary config. Gateway on 8803, then stopped.
Verify:    session 20261027T150920Z_t3loop0210ce2142. Three provider responses, tool_requests empty. Responses 2 and 3 each report output_tokens 60000, all thinking. Interventions empty_completion retry, then exhausted. Turn error: Empty model completion after recovery.
Catalog:   T3-LOOP-02 stays empty.
Recorded failures not fixed: none
Open questions: none. Omitting model.max_tokens still sends the provider default of 60000.
Next:      P3-02 stays open for T3-CTX-01 and T3-LOOP-02.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-LOOP-02 on minimax-m3.1-flash
Status:    blocked (tool calls completed; no truncated batch)
Changed:   no cassette and no src/monkeybot change. Temporary gateway model ollama-cloud/minimax-m3.1-flash, thinking_budget -1, no custom max_tokens. Gateway on 8803, then stopped.
Verify:    First session 20261027T152646Z_t3loop020376c074 failed with Provider request failed (ReadError) and partial_chars 0. Retry session 20261027T152957Z_t3loop029547ef16 wrote essay.txt twice (160770 then 300504 bytes), ran a count command, then Error Max turns exceeded. No truncated_batch intervention.
Catalog:   T3-LOOP-02 stays empty.
Recorded failures not fixed: none
Open questions: none
Next:      P3-02 stays open for T3-CTX-01 and T3-LOOP-02.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-LOOP-02
Status:    deferred (user 2026-10-27)
Changed:   regression/plan/status.md and docs/testing.md. No cassette and no src/monkeybot change.
Verify:    n/a
Catalog:   T3-LOOP-02 Expect notes the deferral. Test cell stays empty. Listed under Deferred coverage. T2-LOOP-04 remains the rejection check.
Recorded failures not fixed: none
Open questions: none
Next:      P3-02 stays open for T3-CTX-01.

### 2026-10-27 P5-02 cursor
Task:      P5-02 First baselines (live)
Status:    in-progress
Changed:   regression/plan/status.md only. No src/monkeybot change. The earlier smoke run was aborted at trial 1/8 when its shell died and the 8797 gateway shut down. No baseline file was written. A detached driver restarted the full suite list from smoke.
Verify:    not yet. Gateway 8797 health 200. Driver pid 55685. First smoke trial of the aborted run was 11/12; file/core_write failed max_tool_errors 1 > 0.
Catalog:   n/a
Recorded failures not fixed: file/core_write max_tool_errors 1 > 0 on the aborted trial 1. Not fixed.
Open questions: none
Next:      the driver continues smoke, then coding, coding_hard, subagents, memory, mcp, both drift configs, then a smoke N=2 compare. P5-03 after that.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live), T3-CTX-01
Status:    in-progress (T3-CTX-01 replays; T3-LOOP-02 stays deferred)
Changed:   Summarizer streams are written to the transcript with exchange_id and rebuilt into the cassette in request order. T4-CTX-01 context window is 10000. Live session 20261027T153149Z_d128083b-1f11-4a7e-bff7-1524816b4da7 (72 summarizations) converted to regression/t3/trajectories/ctx/replay_live_compaction.cassette.json. Replay uses the same window and model.max_turns 25. grep schema lists path, which the executor already accepts as an alias of root. The live scenario approves up to 12 unlisted command confirmations so the recording does not wait out the pending-response timeout.
Verify:    Live T4-CTX-01 passed (5 messages, 72 summarizations). uv run pytest regression/t3/trajectories/ctx/replay_live_compaction.yaml -q → passed, twice. Digest warnings only: tool_schema_digest and live-only tools the loop driver does not advertise.
Catalog:   T3-CTX-01 → regression/t3/trajectories/ctx/replay_live_compaction.yaml. T3-LOOP-02 stays deferred.
Recorded failures not fixed: test-runs/20261027T155350Z/20261027T155350Z-8248de8-56027.jsonl T3-CTX-01 cassette_stale: tool grep changed. The follow-up replay passed after the schema listed path.
Open questions: none
Next:      P3-02 stays open only because T3-LOOP-02 is deferred. P5-02 live baselines are the other in-progress task.

### 2026-10-27 P3-02 cursor
Task:      P3-02 Record the T3a cassettes (live)
Status:    done
Changed:   status.md only. The four recorded cassettes already replay. T3-LOOP-02 stays deferred, so it is not a fifth cassette.
Verify:    no new run. T3-CTX-01 replay was already green.
Catalog:   T3-CTX-01, T3-VER-01, T3-LOOP-01, and T3-SHELL-01 replay. T3-LOOP-02 stays deferred.
Recorded failures not fixed: none
Open questions: none
Next:      P5-02. Phase 3 is done.

### 2026-10-27 T3-LOOP-02 cursor
Task:      T3-LOOP-02 replay live truncation
Status:    done
Changed:   regression/t3/trajectories/loop/replay_live_truncation.{cassette.json,yaml} from a live gemma4:31b turn (session 20261027T161455Z_t3loop02gemma1). thinking off, max_tokens 120, max_turns 1. docs/testing.md Test cell filled and the deferral removed. Pairing seeds 411 filled rows and 435 test paths. No src/monkeybot change. Temporary gateway on 8804.
Verify:    uv run pytest regression/t3/trajectories/loop/replay_live_truncation.yaml -q → passed. Digest warnings only.
Catalog:   T3-LOOP-02 → regression/t3/trajectories/loop/replay_live_truncation.yaml.
Recorded failures not fixed: none
Open questions: none
Next:      P5-02

### 2026-10-27 P6-01 cursor

Task:      P6-01 Cutover, partial (old paths only)
Status:    in-progress
Changed:   Removed the leftover evals/ tree (knowledge_layer, runs, egg-info, TEST_COVERAGE.md, local venv, caches). 125 tracked evals files are deleted in the worktree and not committed. Dropped /evals/runs/ from .gitignore. Rewrote docs/live-evals.md as the T4 runbook and pointed README, CONTRIBUTING, getting-started, features, verifier-agent-design, and the smoke baseline note at regression/t4. docs/testing.md §2 now matches ci-local.sh and D3. Phase 5 was not deferred. T5-GW-01 and T5-GW-02 Test cells were left empty.
Verify:    evals/, tests/evals/, tests/integration/, tests/acceptance/, and tests/profiling/ are absent. uv run pytest tests/contracts/test_catalog_pairing.py -q → 1 passed. uv run pytest regression/t2/loop/scenarios/file/tool_glob_grep.yaml -q → 1 passed. ci-local.sh and the live smoke were not run.
Catalog:   unchanged. T5-GW-01 and T5-GW-02 stay empty until P5-03, or until Phase 5 is deferred and those rows are listed in docs/testing.md §9.
Recorded failures not fixed: none
Open questions: none
Next:      P6-01 stays open until P5-03 is done or Phase 5 is deferred, then the full gate and one live smoke. P5-02 is still in progress.
Left on purpose: tests/contracts/test_catalog_pairing.py imports tests.contracts (current package). tests/contracts/test_migrations_replay.py names tests/core/test_session_branches_migration.py as the path in commit 164b76cd. tests/unit/core/fixtures/bitbucket_dc_style_pr.diff is stored diff text a test reads.

### 2026-10-27 P6-01 cursor
Task:      P6-01 Cutover
Status:    done
Changed:   Phase 5 marked deferred (P5-02, P5-03). Stopped the live baseline processes. docs/testing.md §9 lists T4-RT-01, T3-PROV-01..05, the non-Ollama families of T3-PROV-06, and the Phase 5 deferral. scripts/ci-local.sh clears pytest addopts for browser-mcp so it does not load regression.plugin. No src/monkeybot change.
Verify:    bash scripts/ci-local.sh → exit 0. Deterministic pytest 4255 passed, 54 skipped. cli 357 passed. browser-mcp 320 passed, 18 skipped. ruff format clean. mypy clean. cassette staleness ok. Live smoke was not run (no confirmation; Phase 5 live work was deferred).
Catalog:   empty Test cells are T4-RT-01, T3-PROV-01..05. T5-GW-01 and T5-GW-02 have Test cells. Listed in §9.
Recorded failures not fixed: test-runs/20261027T163137Z/20261027T163137Z-8248de8-64024.jsonl T5-GW-01 TimeoutError; T5-GW-02 AssertionError (no ToolCallStarted call id).
Open questions: none
Next:      none. Phase 5 stays deferred.
Left on purpose: tests/contracts/test_catalog_pairing.py imports tests.contracts. tests/contracts/test_migrations_replay.py names the old tests/core path as history. tests/unit/core/fixtures/bitbucket_dc_style_pr.diff is fixture diff text.
