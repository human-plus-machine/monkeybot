# Phase 0b — Regression foundations (≈ 3–4 days)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

- Create `regression/` layout, `conftest.py`, markers, YAML collector.
- Upgrade `ScriptedFakeProvider` into multi-turn strict `ScriptedProvider` in `src/monkeybot/core/testing/`; `MODEL_PROVIDER=fake` reads `MONKEYBOT_FAKE_PROVIDER_SCRIPT`.
- `regression/drivers/loop.py`, `regression/schema/invariants.py`, unified `schema/scenario.py` and `schema/assertions.py` on top of the `testkit` from Phase 0a.
- Migrate the 19 `tests/evals` scenarios.
- Add `regression` to `pytest.ini` `testpaths`, `.pre-commit-config.yaml`, and `scripts/ci-local.sh`.
- Create `docs/testing.md` with all nine sections (5.10). Generate §8 from section 6 of this plan (`reference/06-coverage/*.md`): every case ID as a row, Expect from the "Asserts" column, default priority by area, Test cell filled for the 19 migrated scenarios and empty for the rest. Add `tests/contracts/test_catalog_pairing.py` and the `case:` key / `@pytest.mark.case` on the migrated scenarios.

Exit: the 19 migrated scenarios pass under the new runner with global invariants on; `uv run pytest regression -m "t1 or t2 or t3" -n auto` < 2 min; the pairing test is green and reports the uncovered-row count.

## Tasks

Order: P0B-01 and P0B-02 in parallel → P0B-03 → P0B-04 → P0B-05 → P0B-06 → P0B-07.

---

### P0B-01 — Multi-turn strict `ScriptedProvider` (planned source seam)

- **Depends on:** P0A-06
- **Blocked by:** none
- **Read:**
  - [05.1-doubles.md](../reference/05.1-doubles.md), the `ScriptedProvider` row only;
  - [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md), the `script` row only;
  - `src/monkeybot/core/testing/mocks_provider.py`, and the code that resolves `MODEL_PROVIDER=fake` (`rg -n '"fake"' src/monkeybot`).
- **Creates / changes:** `src/monkeybot/core/testing/mocks_provider.py`, `testkit/provider.py` (re-export), `tests/unit/testkit/test_scripted_provider.py`
- **Steps:**
  1. Add `ScriptedProvider`, with one event list per `stream()` call and the full event vocabulary in §5.1.
  2. Add the `raise` step: an exception class, optionally after N events.
  3. Record `stream_calls`, `stream_messages`, `stream_tools`, `stream_models`, the `count_input_tokens` calls, and the call hints.
  4. Make strict mode the default: a call past the end of the script raises a clear error.
  5. With `MODEL_PROVIDER=fake` and `MONKEYBOT_FAKE_PROVIDER_SCRIPT=<path>`, load the script from YAML or JSON. Without the variable, behavior stays exactly as today.
  6. Keep `ScriptedFakeProvider` working unchanged (an alias or a thin wrapper) so existing tests do not change.
- **Verify:**
  - `uv run pytest tests/unit/testkit tests/unit/core/testing -q`
  - `uv run pytest tests -q -n auto` (unchanged)
  - `uv run mypy src/monkeybot testkit`
- **Done when:** new tests cover multi-turn, strict overrun, `raise`, the env script, and that the existing behavior is unchanged.
- **Stop and ask if:** existing tests rely on today's behavior of yielding nothing once the script is exhausted.

---

### P0B-02 — Scenario schema (models only)

- **Depends on:** P0A-06
- **Blocked by:** none
- **Read:** [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md) (all); [03-tiers.md](../reference/03-tiers.md)
- **Creates / changes:** `regression/schema/{__init__,scenario,events,steps}.py`, `regression/selftest/{__init__,test_scenario_schema}.py`
- **Steps:**
  1. One Pydantic `Scenario` model whose `mode` decides which keys are legal. Unknown keys are an error.
  2. `id` is derived from the file path; `case` is required.
  3. `events.py` maps the YAML event vocabulary to `ProviderEvent` objects.
  4. `steps.py` defines the step models (execution comes in P1-03).
  5. Write tests for: unknown keys, a key used in the wrong mode, a missing `case`, an `id` mismatch, and `invariants_skip` without a reason. (The `verifier_on`/`verifier_off` load error is an assertion rule; P0B-03 tests it.)
- **Verify:** `uv run pytest regression/selftest -q`; `uv run mypy testkit regression/schema`
- **Done when:** every rule in the §5.2 table has a passing or failing test as appropriate.
- **Stop and ask if:** none expected.

---

### P0B-03 — Assertion engine and global invariants

- **Depends on:** P0B-02
- **Blocked by:** none
- **Read:**
  - [05.3-assertions-and-invariants.md](../reference/05.3-assertions-and-invariants.md) (all);
  - `evals/assertions.py`;
  - the assertion half of `tests/evals/scenario_runner.py`.
- **Creates / changes:** `regression/schema/assertions.py`, `regression/schema/invariants.py`, `regression/selftest/test_assertions.py` (P4-01 later merges `evals/test_assertions.py` into it), `regression/selftest/test_invariants.py`
- **Steps:**
  1. Implement every key in §5.3.1, with the changes the table lists:
     - `interventions` is exact and ordered;
     - `tool_categories_used` is a subset check;
     - `max_tool_errors` forgives an error only after a later success on the same canonical target.
  2. Reject, at load time, any key used in the wrong mode, and `verifier_on`/`verifier_off` blocks while `EVAL_VERIFIER_MODE` is unset (§5.3.1).
  3. Implement the 8 invariants of §5.3.2, with `invariants_skip` requiring a reason.
  4. Leave `evals/assertions.py` in place; P4-01 retires it.
- **Verify:** `uv run pytest regression/selftest -q`
- **Done when:** each key and each invariant has a positive and a negative test.
- **Stop and ask if:** an old assertion's semantics conflict with the §5.3 table in a way the migrated scenarios depend on.

---

### P0B-04 — Loop driver, record hook, YAML collector, `regression/plugin.py` and `conftest.py`

- **Depends on:** P0B-01, P0B-03
- **Blocked by:** none
- **Read:**
  - [04-file-structure.md](../reference/04-file-structure.md) §4.4, the top of the tree, `drivers/` and `fixtures/configs/` only; §4.6, the `pytest.ini` bullet only;
  - [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md), the `config`, `workspace_fixture` and `hooks` rows;
  - [05.6-pytest-reporting-trials.md](../reference/05.6-pytest-reporting-trials.md) §5.6;
  - `tests/evals/scenario_runner.py` and `tests/evals/eval_hook.py`.
- **Creates / changes:** `regression/plugin.py`, `regression/conftest.py`, `pytest.ini` (`-p regression.plugin` in `addopts`), `regression/drivers/{__init__,loop}.py`, `regression/drivers/record.py` (`git mv` of `tests/evals/eval_hook.py`; then update the import in `tests/evals/scenario_runner.py`), `regression/fixtures/configs/agent.base.yaml` (the base config every scripted scenario's `config:` merges over)
- **Steps:**
  1. `plugin.py`, loaded for every test root through `-p regression.plugin` in `pytest.ini`:
     - registers the options `--live`, `--record`, `--update-golden` (refused when `CI` is set), `--trials`, `--suite`, `--agent-url`. Register them here only; a second registration in any conftest makes pytest fail with "option names already added";
     - `pytest_collect_file` turns each `*.yaml` under `regression/t2/**/scenarios/` into one item (T3 and T4 tasks extend the glob).
  2. Each item sets `user_properties` `case`, so ledger records carry the case ID.
  3. `conftest.py`: T4/T5 items are deselected unless `--live` is passed. The other options do nothing yet; later tasks wire them.
  4. `drivers/loop.py` builds the real `loop.run` with:
     - the scripted provider;
     - the `recording` or `real` executor;
     - fixture workspace overlays (until P4-01, the coding repos resolve from `evals/fixtures/coding/`, §5.2);
     - config overrides and env;
     - `hooks:` bundles `verification_gate` and `memory` only (other bundles arrive with their area tasks).
     It returns the event list plus the recorded calls, and runs the invariants.
  5. Add one throwaway scenario to prove the path works end to end, then delete it.
- **Verify:** `uv run pytest regression -q`; run a scenario with a broken assertion and confirm that the ledger record has `tier: t2` and the case ID.
- **Done when:** a YAML scenario runs as a pytest item with `-k` and `-n auto`, and invariants run on it.
- **Stop and ask if:** `loop.run` needs a port that has no `testkit` double yet. List it rather than writing one ad hoc.

---

### P0B-05 — Migrate the 19 `tests/evals` scenarios

- **Depends on:** P0B-04
- **Blocked by:** none
- **Read:**
  - [07-migration-map.md](../reference/07-migration-map.md), the `tests/evals/*` rows;
  - [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t2/loop/scenarios/` part only (destination paths);
  - for each scenario's case ID, its row in the area file: `turn_completion` → 6A; `tool_read`, `tool_write`, `tool_replace_apply_patch`, `tool_glob_grep` (T2-FILE-33) → 6D; `tool_run_command` → 6E; `tool_loops` (T2-SCHED-11) → 6L; `coding_*` → 6G; `tool_load_file` → 6H; `subagent_roundtrip` → 6J; `memory_write_inject` (T2-MEM-08), `tool_search_memory` (becomes `mempalace_search_real`, T2-MEM-06) → 6P; `memory_wake_up_large` (T2-CTX-19) → 6B; `tool_web_search` → 6O; `tool_todo_list` and `tool_todo_list_steps` (both T2-TODO-01) → 6S.
- **Creates / changes:** 19 YAML files under `regression/t2/loop/scenarios/<area>/` (via `git mv`, then edited); deletes `tests/evals/test_agent_behavior.py` and `tests/evals/scenario_runner.py` once the 19 YAML items pass (they replace both; `test_agent_behavior.py` hard-codes the old scenario folder, so it cannot be moved)
- **Steps:**
  1. `git mv` each scenario to its §4.4 path and name.
  2. Convert keys: `user_message` → `messages`, `scripted_turns` → `script`, `memory_index` → `memory.index`, `gate: true` → `hooks: [verification_gate]`. Add `case:`.
  3. Keep each scenario's current executor and doubles, even where the area file says "now real executor" or "fake worker". The Phase 1 area task makes that switch, because the fixtures and doubles it needs (P1-01, P1-11, P1-17) do not exist yet.
  4. `tests/evals/test_verifier_port.py` stays until P1-12.
- **Verify:** `uv run pytest regression/t2 -q`. All 19 pass with invariants on, and none has `invariants_skip` without a reason.
- **Done when:** `tests/evals/scenarios/` is empty and 19 items pass.
- **Stop and ask if:** a migrated scenario only passes with an invariant skipped. That is probably a real harness issue. Stop and ask before adding any `invariants_skip`.

---

### P0B-06 — `docs/testing.md`, catalog parser, pairing test

- **Depends on:** P0B-05
- **Blocked by:** none
- **Read:**
  - [05.10-testing-doc.md](../reference/05.10-testing-doc.md) (all);
  - `/Users/kz127/code/monkeyapp/docs/testing.md`, read-only, for layout and tone;
  - [05.9-failure-ledger.md](../reference/05.9-failure-ledger.md) §5.9.4–§5.9.5 (source for the doc's §5);
  - [09-ci.md](../reference/09-ci.md) and [10-policies.md](../reference/10-policies.md) (sources for the doc's §2 and §4);
  - [01-goals-and-principles.md](../reference/01-goals-and-principles.md), goal 6 only (the tier-by-folder pairing rule).
  - Do not read the 23 coverage files by hand; the generator in step 1 reads their tables.
- **Creates / changes:** `docs/testing.md`, `testkit/failures/catalog.py` (stdlib only: the ledger imports it in the `cli/` and browser-mcp environments), `testkit/failure_ledger.py` and `testkit/failures/__main__.py` (catalog lookup), `tests/contracts/{__init__,conftest,test_catalog_pairing}.py`, `tests/unit/testkit/test_failures_cli.py` (the `prompt` Expect test); adds `case:` / `@pytest.mark.case` wherever the 19 migrated scenarios still lack it
- **Steps:**
  1. Write a throwaway generator (not committed) that reads every table in `regression/plan/reference/06-coverage/*.md` and emits the §8 subsections. For each row:
     - ID;
     - Case: the slug, made readable;
     - Expect: the "Asserts" text;
     - Tier: from the prefix;
     - Pri: the area default from §5.10;
     - Origin: the area's `Code:` line;
     - Test: the path for the 19 migrated cases, empty otherwise.
     Range rows such as `T4-CODE-01..03` stay as one row.
  2. Write doc sections 1–7 and 9 from the files listed under Read. Keep the `## 8.` and `## 9.` headings exactly as named; the parser relies on them.
  3. `catalog.py` parses §8 and supports single IDs and `..` ranges: a test whose case ID is `T4-CODE-05` covers the row `T4-CODE-04..10`. The pairing test enforces the four rules in §5.10 and prints uncovered counts per area and tier.
  4. Point the ledger and `testkit.failures prompt` at `catalog.py` for `catalog_expect`.
- **Verify:**
  - `uv run pytest tests/contracts/test_catalog_pairing.py -q` passes and prints the uncovered counts.
  - Rename a migrated scenario's `case:` to a bogus ID: the test fails. Revert.
  - `uv run pytest tests/unit/testkit -q`, including the new test that `prompt` prints the catalog Expect text (the §5.9.6 bullet P0A-01 left out).
- **Done when:** the doc has all nine sections; every case ID from §6 has a row; the pairing test is green.
- **Stop and ask if:** a §6 table row cannot be parsed into a single ID or a range.

---

### P0B-07 — Wire `regression/` into the gates; phase exit

- **Depends on:** P0B-06
- **Blocked by:** none
- **Read:** this phase file's Summary; [09-ci.md](../reference/09-ci.md), the pre-commit and pre-push rows only
- **Creates / changes:** `scripts/ci-local.sh`, `.pre-commit-config.yaml` (if not already done in P0A-05)
- **Steps:**
  1. Confirm that the pre-push gate runs `tests regression -m "t1 or t2 or t3"`.
  2. Time it.
  3. Check every Summary exit bullet.
- **Verify:** `time uv run pytest regression -m "t1 or t2 or t3" -q -n auto` takes under 2 minutes; `bash scripts/ci-local.sh` passes.
- **Done when:** the Summary exit criteria hold.
- **Stop and ask if:** the time budget is blown.
