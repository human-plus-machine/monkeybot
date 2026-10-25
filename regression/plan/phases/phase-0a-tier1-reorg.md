# Phase 0a — Tier-1 reorganization and `testkit` (≈ 2 days)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

Mechanical, no behavior change. It goes first so every later phase writes against the final paths. One PR per task unless the user groups them (AGENT_GUIDE §13.5).

0. Land the failure ledger first, so failures during the migration itself are recorded and agents doing the migration stop at them: `testkit/failure_ledger.py`, `testkit/scrub.py`, `testkit/failures/`, `tests/unit/testkit/test_failure_ledger.py`, `test_failures_cli.py`, `test_scrub.py`, the `pytest.ini` `-p testkit.failure_ledger` entry, `.gitignore`, `.cursor/rules/test-failures.mdc`, and the `postToolUse` hook. (`testkit/` is created here with just these files.)
1. Record the baseline in `test-runs/phase0a/` (P0A-01 step 1): collected node IDs (3,320 on 2026-10-25) and a full run's pass/fail/skip set.
2. Create `testkit/` and `git mv` the shared helpers into it: `tests/core/memory/helpers.py` → `testkit/memory.py`, `tests/core/memory/in_memory_palace.py` → `testkit/palace.py`. Cut the helper classes and functions that other modules import out of `test_loop.py`, `test_core_tool_executor.py`, `test_hooks.py`, `test_sandbox_executor.py`, and `tests/providers/conftest.py` into the matching `testkit` modules (section 4.2). Collapse duplicates only when they are behavior-identical; otherwise keep both under distinct names and leave the merge to D7.
3. `git mv` every test file to its section 4.3 location. Add `__init__.py` to every new folder.
4. Rewrite the 66 `from tests.…` imports (45 files) to `from testkit…`. Afterwards the only `from tests.` imports left are the two inside `tests/evals/`, which go away in P0B-05.
5. Move `tests/integration/*` and `tests/acceptance/*` to `regression/t2/python/` and swap their markers to `t2`. This is the only part of 0a that creates `regression/`.
6. Update `pytest.ini`, `.pre-commit-config.yaml`, `scripts/precommit-pytest.sh`, `scripts/ci-local.sh`, and the ruff/mypy paths in `pyproject.toml` (section 4.6).
7. Delete the emptied folders (`tests/core/`, the old top-level `tests/<area>/` folders, `tests/profiling/`).

Exit:

- `uv run pytest tests regression --co -qq` lists the same tests as `test-runs/phase0a/before_ids.txt`. Node IDs change only by path prefix, so the check is: the same count, and a 1:1 mapping after the old→new path substitution.
- A full run gives the same pass/fail/skip set.
- `ruff`, `mypy`, and `scripts/ci-local.sh` are green.
- `git log --follow` works on a sample of moved files, so history is preserved.
- The ledger is on: a deliberately failing test produces a record, the banner, and the hook notice, and the exit code is unchanged.

## Tasks

Order: P0A-01 → P0A-02 → P0A-03 → P0A-04 → P0A-05 → P0A-06. P0A-02 can run in parallel with P0A-03.

Phase-wide rules:

- Test bodies and assertions do not change in this phase. Only files move, helper code moves, imports change, and path arithmetic that depends on folder depth is adjusted.
- Adding type annotations to helper code moved into `testkit/` is allowed (mypy runs in strict mode); behavior must not change. If a module cannot be typed without changing it, add a `[[tool.mypy.overrides]]` entry for that module with `disallow_untyped_defs = false` and list it in the report.
- "Same pass/fail set" compares node outcomes with `test-runs/phase0a/before_results.txt` after the old → new path substitution. If one node differs, re-run it alone with `-p no:xdist`. If it then matches the baseline, list it as flaky in the report and treat it as unchanged. If it still differs, stop and ask.

---

### P0A-01 — Failure ledger: plugin, scrubber, triage CLI

- **Depends on:** none
- **Blocked by:** none
- **Read:** [05.9-failure-ledger.md](../reference/05.9-failure-ledger.md) (all); [04-file-structure.md](../reference/04-file-structure.md) §4.2 and §4.6 only; [05.5-cassettes.md](../reference/05.5-cassettes.md) §5.5.2, the "Scrub before write" bullet only; [01-goals-and-principles.md](../reference/01-goals-and-principles.md) goal 6 only (the tier-by-folder rule)
- **Creates / changes:**
  - `testkit/__init__.py` (empty, and it stays empty: the ledger plugin must import without `monkeybot` in the `cli/` and browser-mcp environments), `testkit/py.typed`, `testkit/scrub.py`, `testkit/failure_ledger.py`
  - `testkit/failures/__init__.py`, `__main__.py`, `store.py` (`catalog.py` comes in P0B-06)
  - `tests/unit/__init__.py`, `tests/unit/testkit/{__init__,test_failure_ledger,test_failures_cli,test_scrub}.py`
  - `pytest.ini` (`addopts`), `.gitignore` (`/test-runs/`)
- **Steps:**
  1. Add `/test-runs/` to `.gitignore`. Capture the baseline the whole phase compares against:
     - `mkdir -p test-runs/phase0a`
     - `uv run pytest tests --co -qq > test-runs/phase0a/before_ids.txt` (`-qq` prints one node ID per line despite `addopts = -v`)
     - `uv run pytest tests -q -n auto -rA > test-runs/phase0a/before_results.txt`
     - Note the summary line (3,320 collected on 2026-10-25) in the handoff entry in `status.md`.
  2. `testkit/scrub.py`, stdlib only. It redacts:
     - the patterns in §5.5.2 (`Authorization`, `x-api-key`, `x-goog-api-key`, `sk-…`, `nvapi-…`, bearer tokens);
     - project ids: the values of `GOOGLE_CLOUD_PROJECT`, `FIREBASE_PROJECT_ID` and `ANTHROPIC_VERTEX_PROJECT_ID` when set;
     - the literal value (8 characters or longer, so `1` or `true` are not redacted everywhere) of every environment variable whose name ends in `_API_KEY`, `_TOKEN` or `_SECRET`, which covers `OLLAMA_API_KEY`.
  3. `testkit/failure_ledger.py`, stdlib only, per §5.9.1–§5.9.4:
     - options `--ledger-dir` and `--no-ledger` (`MONKEYBOT_TEST_LEDGER=0`);
     - writes only on the xdist controller (skip when `config.workerinput` exists);
     - session start: prune runs, mark the previous run `incomplete` if it has no `run_end`, write `run_start`;
     - one `failure` line per failed `pytest_runtest_logreport` (every phase, strict xpass included), per failed `pytest_collectreport`, and on `pytest_internalerror`;
     - session finish: write `run_end`, `outcomes.tsv.gz`, `LATEST.json` and `index.jsonl`;
     - `pytest_terminal_summary` prints the banner, only when something was recorded.
  4. Record fields:
     - `tier` from the `t1`..`t5` markers, falling back to the path rule in §4.3 / goal 6;
     - `case` from a `case` marker or `item.user_properties`, else `null`;
     - `catalog_expect: null` until P0B-06.
  5. Expose a `LedgerWriter` class with the same record format, for the non-pytest T4/T5 runners.
  6. Wrap every hook body so that any exception prints one warning and never changes the session result.
  7. `testkit/failures/` CLI: `latest`, `open`, `show`, `prompt`, `mark`, `export`, `prune` (§5.9.5). `prompt` leaves the Expect line out until the catalog exists.
  8. Tests from §5.9.6 with `pytester`. Enable it with `-p pytester` in `addopts` (step 9). Do not define `pytest_plugins` in any conftest other than `tests/conftest.py`; pytest 9 rejects it in nested conftests.
  9. `pytest.ini`: `addopts = -v -p testkit.failure_ledger -p pytester`.
- **Verify:**
  - `uv run pytest tests/unit/testkit -q`
  - `uv run pytest tests -q -n auto`: same summary as `before_results.txt`, plus only the new `tests/unit/testkit` tests.
  - `uv run python -m testkit.failures latest`
  - `uv run mypy testkit`
  - `uv run ruff format --check .`
- **Done when:** every §5.9.6 bullet has a passing test, except "`prompt` includes the catalog Expect text", which P0B-06 adds; the pre-existing pass/fail set is unchanged; the exit code is identical with and without `--no-ledger` on a red run.
- **Stop and ask if:** xdist does not forward collection errors to the controller in the pinned version (an unverified assumption in §2.3). Do not work around it with per-worker files without asking.

---

### P0A-02 — Agent rule and Cursor hook

- **Depends on:** P0A-01
- **Blocked by:** none
- **Read:** [05.9-failure-ledger.md](../reference/05.9-failure-ledger.md) §5.9.4 only; the existing `.cursor/hooks.json` and `.cursor/hooks/pre-pr.py` (for conventions)
- **Creates / changes:** `.cursor/rules/test-failures.mdc`, `.cursor/hooks/test-failure-notice.py`, `.cursor/hooks.json`, `.gitignore` (`/.cursor/hooks/.state/`)
- **Steps:**
  1. Write `.cursor/rules/test-failures.mdc` with frontmatter (`description`, `alwaysApply: true`) and the five bullets of §5.9.4 item 2. Keep it under 25 lines.
  2. Write `test-failure-notice.py`, Python 3 stdlib only, executable:
     - read the hook JSON from stdin;
     - continue only for test commands (`pytest`, `regression.t4`, `regression.t5`, `ci-local.sh`, `git push`);
     - read `test-runs/LATEST.json`, compare it with `.cursor/hooks/.state/last_notified`, and print `{"additional_context": "..."}` only for a new run that has failures;
     - always exit 0.
  3. Add a `postToolUse` entry with matcher `Shell` to `.cursor/hooks.json`. Keep both `pre-pr.py` entries unchanged.
  4. Log one real hook payload to a temp file first, to confirm which field holds the command. The payload shape is not verified in this plan.
- **Verify:**
  - Pipe three sample payloads through the script: a failing test run, a passing run, and a non-test command. It should print the notice, nothing, and nothing.
  - Then ask the user to run a failing test in an agent session and confirm that the notice appears (Cursor Hooks output channel).
- **Done when:** the samples behave as above and the user has confirmed the live notice.
- **Stop and ask if:** the `postToolUse` payload has no command field, or the notice does not reach the agent.

---

### P0A-03 — Move shared helpers into `testkit` (tests stay where they are)

- **Depends on:** P0A-01
- **Blocked by:** none
- **Read:** [05.1-doubles.md](../reference/05.1-doubles.md) (table); [04-file-structure.md](../reference/04-file-structure.md) §4.2 only; [07-migration-map.md](../reference/07-migration-map.md), the helper rows only
- **Creates / changes:** `testkit/{provider,history,executors,inspectors,pending_bus,context,mcp,memory,palace,hooks,verifier,sandbox,conversations,gateway_ports}.py`; `tests/unit/testkit/test_fake_history.py`; the 45 test files that import from other test modules
- **Steps:**
  1. `rg -n '^\s*(from|import) tests\.' tests evals` and group the results by imported symbol. Expect 66 lines across 45 files.
  2. Whole-file helpers: `git mv tests/core/memory/helpers.py testkit/memory.py` and `git mv tests/core/memory/in_memory_palace.py testkit/palace.py`.
  3. Helpers defined inside test modules: move each definition to its `testkit` module, and import it back into the original module if that module still uses it. Keep the original names unless the table says otherwise. Destinations:

     | Symbols imported by other modules (defined in) | `testkit` module |
     |---|---|
     | `FakeProvider` (`test_loop`), `CapturingProvider` (`test_loop_hooks`), `ToolsRecordingProvider` (`test_doom_loop`) | `provider.py` (P0B-01 later adds the `ScriptedProvider` re-export) |
     | `FakeHistory` (`test_loop`) | `history.py` |
     | `RecordingExecutor` (`test_loop`) | `executors.py` |
     | `AllowInspector` (`test_loop`) | `inspectors.py` |
     | `_ctx`, `_ctx_with_task` (`test_loop`), `_ctx` (`test_core_tool_executor`, a different function: name it `tool_ctx` and import it `as _ctx`, so test bodies do not change), `_minimal_ctx` (`test_prompt`), `_stub_agent_md_for_tasks` (`test_core_tool_executor`) | `context.py` |
     | `_NoMCP` (`test_core_tool_executor`, `test_subagent_trace_propagation`), `FakeMCPClient` (`test_context`) | `mcp.py` |
     | `_mem_sub` (`test_core_tool_executor`, `test_subagent_trace_propagation`) | `memory.py` |
     | `_payload` (`test_hooks`) | `hooks.py` |
     | `_payload`, `_mailbox` (`test_progress_tracker`), `ScriptedClassifier` (`test_goal_ledger`) | `verifier.py` (if it differs from the hooks one, name it `verifier_payload` and import it `as _payload`) |
     | `_make_create_mock`, `_make_opensandbox_module`, `_opensandbox_sys_modules` (`test_sandbox_executor`) | `sandbox.py` |
     | `typed_messages_*`, `CANONICAL_TOOL_DEFS`, `make_anthropic_stream_mock` (`tests/providers/conftest.py`) | `conversations.py` |
     | `FakeLoopPort` (`tests/gateway/sse/test_routes`) | `gateway_ports.py` |

     `test_loop_hooks` re-exports `AllowInspector`, `FakeHistory`, `RecordingExecutor` and `_ctx` from `test_loop`; point its importers at `testkit` too. Same-named helpers in two modules are duplicates for step 4. Symbols nobody else imports (for example `PresetPendingBus`, `CountingFakeProvider`) stay where they are.
     A symbol from step 1 that is not in this table goes to the module whose §4.2 description fits; if none fits, stop and ask.
  4. Duplicates (for example the several `FakeHistory` copies): merge only when they behave identically. Otherwise keep them under distinct names and note them for D7.
  5. Rewrite every importer to `from testkit.<module> import …`.
  6. Do not create `clock.py`, `subagent_worker.py`, `web_search.py`, `sse.py`, `gateway.py` or `backends.py` yet; later tasks own them.
  7. Add `tests/unit/testkit/test_fake_history.py`, covering the merged `FakeHistory` (one test per `HistoryStore` method the tests use).
- **Verify:**
  - `rg -n '^\s*(from|import) tests\.' tests evals regression | rg -v '^tests/evals/'` returns nothing. The two `tests.evals.*` imports inside `tests/evals/` stay until P0B-05.
  - `uv run pytest tests -q -n auto` gives the same pass/fail set as `test-runs/phase0a/before_results.txt` (plus the testkit tests).
  - `uv run mypy testkit`
- **Done when:** there are no cross-test-module imports and results are unchanged.
- **Stop and ask if:** two "duplicate" helpers differ in a way that makes some tests depend on one specific copy.

---

### P0A-04 — `git mv` tier-1 tests into `tests/unit/`

- **Depends on:** P0A-03
- **Blocked by:** none
- **Read:** [04-file-structure.md](../reference/04-file-structure.md) §4.3 (the tree and the placement rule) and §4.6 (the `__init__.py` note)
- **Creates / changes:** `tests/conftest.py` (the `pytest_plugins` path only); every file under `tests/core/`, `tests/{gateway,providers,observability,computer,scheduler,todo_list,web_search,cli,skills}/`, and `tests/test_{cold_start,packaging_config,release_script}.py`
- **Steps:**
  1. Turn the §4.3 tree into an explicit old → new list with a throwaway script (not committed). Every `tests/core/` file must map to exactly one destination; 116 test files plus the fixture.
  2. `git mv` each file. Add `__init__.py` to every new folder.
  3. `tests/evals/` stays in place in this phase; it moves in P0B-05.
  4. Fix depth-dependent paths only: `rg -n 'Path\(__file__\)|parents\[|fixtures/' tests`. Adjust `parents[N]` and fixture paths. Change nothing else in the test bodies.
  5. In `tests/conftest.py`, change `pytest_plugins = ["tests.observability.conftest"]` to `["tests.unit.observability.conftest"]`. This is the only change to that file in this task.
- **Verify:**
  - Collect the new IDs (`uv run pytest tests --co -qq`), apply the old → new substitution, and compare with `before_ids.txt`. Count and set must match, except for the new `tests/unit/testkit` tests.
  - Full run: same pass/fail set.
  - `git log --follow` on three moved files shows their history.
- **Done when:** the ID mapping is 1:1 and results are unchanged.
- **Stop and ask if:** a file does not fit any §4.3 folder, or the counts differ and the cause is not a move or path problem.

---

### P0A-05 — Integration and acceptance tests into `regression/`; config and scripts

- **Depends on:** P0A-04
- **Blocked by:** none
- **Read:** [04-file-structure.md](../reference/04-file-structure.md) §4.6 only; [05.6-pytest-reporting-trials.md](../reference/05.6-pytest-reporting-trials.md) §5.6, the markers line only; `.github/workflows/ci.yml`
- **Creates / changes:**
  - `regression/__init__.py`, `regression/conftest.py` (minimal: tier marking by path only)
  - `regression/t2/__init__.py`, `regression/t2/python/__init__.py`
  - moved `tests/integration/*` and `tests/acceptance/*` (`tests/acceptance/` has no `__init__.py` today; the destination folder gets one)
  - `tests/unit/conftest.py`; `tests/conftest.py` (marks `tests/evals/` items `t2` until P1-12 empties that folder)
  - `pytest.ini`, `.pre-commit-config.yaml`, `scripts/precommit-pytest.sh`, `scripts/ci-local.sh`, `pyproject.toml`, `.github/workflows/ci.yml`
- **Steps:**
  1. `git mv` the three integration tests and the acceptance test into `regression/t2/python/`, using the names in §4.4. Replace their `integration` / `acceptance` markers with `t2`. Fix their `parents[N]` arithmetic (both observability files use `parents[2]`), and nothing else in their bodies.
  2. `tests/unit/conftest.py` and `regression/conftest.py` add the tier marker by path in `pytest_collection_modifyitems`. In `tests/conftest.py`, mark items under `tests/evals/` as `t2`, so they stay in the `-m "t1 or t2 or t3"` gate until P1-12 removes the last of them; P1-12 also removes this marking.
  3. `pytest.ini`:
     - `testpaths = tests regression`;
     - register `t1`–`t5`, `live`, `needs_postgres`, `needs_firestore`, `darwin_only`, `slow`, `quarantine`, `case`;
     - drop `integration` and `acceptance`.
  4. `.pre-commit-config.yaml` `files:` regex and `precommit-pytest.sh` collection fallback as in §4.6.
  5. `ci-local.sh`:
     - export `MONKEYBOT_TEST_RUN_GROUP` and `MONKEYBOT_TEST_TRIGGER`;
     - change the pytest line to `tests regression -m "t1 or t2 or t3"`;
     - add `-p testkit.failure_ledger --ledger-dir "$root/test-runs"` with `PYTHONPATH="$root"` to the `cli/` and `integrations/browser-mcp/` pytest calls;
     - change mypy to `src/monkeybot testkit`.
  6. `pyproject.toml`: there are no path lists today. Add `files = ["src/monkeybot", "testkit"]` under `[tool.mypy]`, and set `known-first-party = ["monkeybot", "testkit", "regression"]` under `[tool.ruff.lint.isort]`. The `evals` extra stays.
  7. `.github/workflows/ci.yml`: make the pytest and mypy steps match `ci-local.sh` (`tests regression -m "t1 or t2 or t3"`; `mypy src/monkeybot testkit`). Leave the evals self-check step alone; P4-01 changes it.
- **Verify:**
  - `uv run pytest -m t1 --co -qq | wc -l` equals the P0A-04 collected count minus the four moved tests.
  - `uv run pytest -m t2 --co -q` lists the four moved tests.
  - `bash scripts/ci-local.sh` passes.
- **Done when:** the gate passes on the new paths and the four moved tests carry `t2`.
- **Stop and ask if:** `ci-local.sh` fails for a reason outside these edits. Record it and report it; do not fix it.

---

### P0A-06 — Phase 0a exit check and cleanup

- **Depends on:** P0A-02, P0A-05
- **Blocked by:** none
- **Read:** this phase file's Summary only
- **Creates / changes:** deletes the emptied folders `tests/core/`, the old `tests/<area>/` folders, `tests/integration/`, `tests/acceptance/` and `tests/profiling/` (keep `tests/evals/`)
- **Steps:**
  1. Delete the empty folders. `git status` must show only deletions of folders with no tracked files left in them.
  2. Re-run every exit bullet in the Summary.
  3. Run one deliberately failing throwaway test, and then delete it:
     - a ledger record is written and the banner prints;
     - the hook notice appears;
     - the exit code is non-zero.
- **Verify:**
  - `rg -n 'tests/core/|from tests\.' --glob '!regression/plan/**' --glob '!CHANGELOG.md' --glob '!docs/**' --glob '!src/**' --glob '!evals/**' --glob '!tests/evals/**' .` finds nothing. Hits in `docs/`, `src/` (docstrings) and `evals/` are expected: list them in the report and do not edit them; P6-01 handles them.
  - `bash scripts/ci-local.sh`
- **Done when:** every Summary exit bullet holds.
- **Stop and ask if:** any exit bullet fails.
