# Phase 1 — Harness logic at T2 (≈ 1.5–2 weeks)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

Order by blast radius: `LOOP`, `FILE`, `SHELL`, `POL`, `CODE`, `HOOK`, `CTX`, then `SUB`, `VER`, `SCHED`, `MCP`, `MEM`, `SKILL`, `TODO`, `ATT`, `WEB`, `COMP`.

- New fixtures: `hostile_ws`, `grants_ws`, policy configs, MCP config variants, `drift_ws`.
- `testkit` additions: fake subagent worker, `ScriptedClassifier`, `ScriptedJudge`, fake web search, `FakeClock`.
- `steps:` actions.

Exit: every `T2-*` case in those areas exists, and each one passes or is a reported harness discrepancy (step 3 of the area procedure below) that the user has ruled on; drift confusion matrix (T2-VER-23) at 1.0, or its misses reported the same way; their catalog rows have Test paths. Agents never add `quarantine`, `skip` or `xfail` themselves (AGENT_GUIDE §13.4).

## Tasks

Order:

- P1-01, P1-02 and P1-03 come first; every area task depends on them.
- Area tasks P1-04 … P1-17 can run in any order once their own dependencies are done. The order below is the blast-radius order from the Summary.
- P1-18 closes the phase.

### How every area task works (P1-04 … P1-17)

Every area task reads, in addition to its own list:

- [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md) and [05.3-assertions-and-invariants.md](../reference/05.3-assertions-and-invariants.md), for the scenario keys and assertions;
- [04-file-structure.md](../reference/04-file-structure.md) §4.4, **only the subtree for its area** (`t2/loop/scenarios/<area>/`, plus any `t2/python/` files it names), for file names and paths;
- its area file in `reference/06-coverage/`, **only the T2 rows** (T1, T3 and T4 rows belong to other phases).

Then, for each case ID in the task:

1. Read the code the case targets (the area file's `Code:` line) until you can write the expected event sequence and state.
2. Write the scenario at its §4.4 path, with `case:` set. Use `executor: recording` with `tool_results` when the case is about loop logic, and `executor: real` with a fixture workspace when it is about tool behavior.
3. Run it. If it fails, decide which applies:
   - **the scenario is wrong:** fix the scenario;
   - **a schema, assertion, or `steps:` feature is missing:** add it only if the task lists it, otherwise stop and ask;
   - **the harness really behaves differently from the "Asserts" column:** do not change `src/` and do not change the expectation. Leave the case failing (it is in the ledger), set its row's Test cell anyway, and continue with the next case. When the rest of the area is done, set the task to `blocked` in `status.md` and list each discrepancy (case ID, fingerprint, one-line error) as a question for the user. A failing case makes the pre-push gate red, so the user decides per case: a `src/` fix as separate work, or a `quarantine` marker (issue link and expiry, §10.4) that the user adds or explicitly tells you to add.
4. Fill the case's Test cell in `docs/testing.md` §8.

Area task **Verify** is always:

- `uv run pytest <the task's scenario folders> <the task's regression/t2/python files> -q -n auto`, using the exact paths in its **Creates** line (`-k <area>` does not work: every scenario path contains `loop/`)
- `uv run pytest tests/contracts/test_catalog_pairing.py -q`
- `uv run pytest tests -q -n auto` (unchanged)

Area task **Done when** is always:

- every case in the range exists and has its Test cell filled;
- every case passes, or is a reported harness discrepancy per step 3 (in which case the task is `blocked` until the user rules, not `done`);
- no `invariants_skip` without a reason.

---

### P1-01 — New fixtures

- **Depends on:** P0B-07
- **Blocked by:** none
- **Read:** [05.4-fixtures.md](../reference/05.4-fixtures.md) (all); [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `fixtures/` subtree only
- **Creates / changes:**
  - `regression/fixtures/build.py`, `regression/fixtures/workspaces/{hostile_ws,grants_ws,drift_ws}/`
  - `regression/fixtures/configs/*`, `regression/fixtures/skills/*`, `regression/fixtures/subagent_tapes/` (empty placeholder)
  - `testkit/data/attachments/*`
  - `regression/selftest/test_fixtures.py`
- **Steps:**
  1. Commit small text fixtures as files.
  2. Generate the rest once per session into a `tmp_path_factory` directory with `regression/fixtures/build.py`: the > 512 KB file, the 301-entry directory, the binary file, and the symlinks. `drivers/loop.py` overlays those generated parts when a scenario's `workspace_fixture` is `hostile_ws`.
  3. `command_allowlist.empty.yaml` must make config loading raise.
- **Verify:** `uv run pytest regression/selftest/test_fixtures.py -q` checks each property in the §5.4 table (symlink leaves the root, `.gitignore` is honored, the size limits are exceeded, and so on).
- **Done when:** every fixture in the §5.4 table exists or is built, and is checked, except `legacy_db` (P2-07) and the moved `evals/fixtures/coding` repos (P4-01).
- **Stop and ask if:** a fixture cannot be expressed portably (for example symlinks on a filesystem that lacks them).

### P1-02 — `FakeClock` and the `now_ms` seam (planned source seam)

- **Depends on:** P0B-07
- **Blocked by:** none
- **Read:** [05.1-doubles.md](../reference/05.1-doubles.md), the `FakeClock` row only; the scheduler engine and `list_due`, background-summary scheduling, and stale-claim window code (`rg -n 'time\.time\(\)|monotonic\(\)|now_ms' src/monkeybot/scheduler src/monkeybot/core`)
- **Creates / changes:** `testkit/clock.py`, `tests/unit/testkit/test_clock.py`, minimal `now_ms` / clock parameters in those `src/` functions (each defaulting to the real clock)
- **Steps:**
  1. Add an optional clock parameter at the narrowest call sites. Do not change any default behavior.
  2. `FakeClock` supports `advance(ms)` and is what the `advance_clock` step calls.
- **Verify:** `uv run pytest tests -q -n auto` (unchanged); `uv run mypy src/monkeybot testkit`
- **Done when:** the scheduler, background summary, and stale-claim logic can each be driven by `FakeClock` in a unit test.
- **Stop and ask if:** a seam would need more than a parameter with a default (for example a new global).

### P1-03 — `steps:` execution and `PresetPendingBus`

- **Depends on:** P1-02
- **Blocked by:** none
- **Read:** [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md), the `steps` row only; `testkit/pending_bus.py`; how `loop.run` receives cancel, steer, queue and confirmations (`rg -n 'cancel|steer|pending' src/monkeybot/core/runtime/loop.py`)
- **Creates / changes:** `regression/schema/steps.py` (execution), `regression/drivers/loop.py`, `testkit/pending_bus.py` (move `PresetPendingBus` out of `tests/unit/core/runtime/test_loop.py`, where P0A-03 left it because nothing else imported it, and import it back there; then extend it), `regression/selftest/test_steps.py`
- **Steps:**
  1. Implement each step, triggered by an event predicate: `cancel`, `steer`, `queue_follow_up`, `promote_follow_up`, `answer_ask_user`, `confirm_tool` (approve, deny, always), `touch_file`, `advance_clock`, `reload_config`.
  2. `disconnect_sse` and `reconnect_sse` belong to the gateway driver (P2-01); reject them in the loop driver with a clear error.
- **Verify:** `uv run pytest regression/selftest/test_steps.py -q`, with one tiny scenario per step.
- **Done when:** every loop-driver step fires at its predicate and is visible in the events.
- **Stop and ask if:** `loop.run` has no public way to inject a step (for example steer). List it; do not reach into private state.

---

### P1-04 — `LOOP` (T2-LOOP-02 … T2-LOOP-40)

- **Depends on:** P1-03, P1-14 (T2-LOOP-29 uses the MCP fixture server) · **Blocked by:** none
- **Read:** [6A-loop.md](../reference/06-coverage/6A-loop.md); [appendices.md](../reference/appendices.md) Appendix C
- **Creates:** `regression/t2/loop/scenarios/loop/*.yaml` (39 files)
- **Notes:** T2-LOOP-28 (concurrency cap) may need an executor that records overlap. Add it to `testkit/executors.py` if so.

### P1-05 — `FILE` (T2-FILE-02 … T2-FILE-32, except 08 and 17)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6D-file.md](../reference/06-coverage/6D-file.md); [appendices.md](../reference/appendices.md) Appendix A
- **Creates:** `regression/t2/loop/scenarios/file/*.yaml`, `regression/t2/python/test_grep_parity.py` (T2-FILE-27)
- **Notes:**
  - T2-FILE-20 is two files (`patch_add_existing`, `patch_move_dest_exists`). List both in its Test cell.
  - T2-FILE-01, 08, 17 and 33 were migrated in P0B-05. Switch 01, 08 and 17 to `executor: real` with a fixture workspace, as their rows say; do not create new files for them.

### P1-06 — `SHELL` (T2-SHELL-02 … T2-SHELL-17)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6E-shell.md](../reference/06-coverage/6E-shell.md); [appendices.md](../reference/appendices.md) Appendix A
- **Creates:** `regression/t2/loop/scenarios/shell/*.yaml`, `regression/t2/python/test_jail.py` (T2-SHELL-17)
- **Notes:** the sandbox paths stay on the recording executor; nothing here needs OpenSandbox running.

### P1-07 — `POL` (T2-POL-01 … T2-POL-11)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6F-pol.md](../reference/06-coverage/6F-pol.md)
- **Creates:** `regression/t2/loop/scenarios/policy/*.yaml`
- **Notes:** add the `policy:` key wiring (real inspectors from fixture configs) to `drivers/loop.py`.

### P1-08 — `CODE` (T2-CODE-05 … T2-CODE-18)

- **Depends on:** P1-03 · **Blocked by:** none
- **Read:** [6G-code.md](../reference/06-coverage/6G-code.md)
- **Creates:** `regression/t2/loop/scenarios/coding/*.yaml`
- **Notes:** adds the `git_context` and `read_ledger_compaction` hook bundles to the driver.

### P1-09 — `HOOK` (T2-HOOK-01 … T2-HOOK-15)

- **Depends on:** P1-03 · **Blocked by:** none
- **Read:** [6C-hook.md](../reference/06-coverage/6C-hook.md)
- **Creates:** `regression/t2/loop/scenarios/hook/*.yaml` (HOOK-08..15), `regression/t2/python/test_hooks_lifecycle.py` (HOOK-01..07), `testkit/hooks.py` additions
- **Notes:** adds the `evidence_guard` and `secret_scanner` bundles. Settlement waits must be explicit; never use sleeps.

### P1-10 — `CTX` (T2-CTX-01 … T2-CTX-18)

- **Depends on:** P1-01 (T2-CTX-14 uses `hostile_ws`), P1-02, P1-03 · **Blocked by:** none
- **Read:** [6B-ctx.md](../reference/06-coverage/6B-ctx.md)
- **Creates:** `regression/t2/loop/scenarios/ctx/*.yaml`
- **Notes:** the background-summary cases use `advance_clock`. T2-CTX-19 was migrated in P0B-05.

### P1-11 — `SUB` (T2-SUB-02 … T2-SUB-17)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6J-sub.md](../reference/06-coverage/6J-sub.md); [05.1-doubles.md](../reference/05.1-doubles.md), the fake-subagent-worker row only
- **Creates:** `testkit/subagent_worker.py`, `regression/fixtures/subagent_tapes/*.ndjson`, `regression/t2/loop/scenarios/subagents/*.yaml`, `regression/t2/python/test_subagent_worker_pool.py` (SUB-15, 16)
- **Notes:** the fake worker is selected with `MONKEYBOT_SUBAGENT_SCRIPT`, which already exists (`src/monkeybot/core/subagents/subagent_proto.py`); no source change is needed. Switch the migrated T2-SUB-01 to the fake worker.

### P1-12 — `VER` (T2-VER-01 … T2-VER-23)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6K-ver.md](../reference/06-coverage/6K-ver.md); [05.1-doubles.md](../reference/05.1-doubles.md), the `ScriptedClassifier` / `ScriptedJudge` rows only
- **Creates:** extends the existing `ScriptedClassifier` and adds `ScriptedJudge` in `testkit/verifier.py` (P0A-03 created the module), `regression/t2/loop/scenarios/verifier/*.yaml`, `regression/t2/python/test_drift_confusion_matrix.py` (VER-23); once all of `tests/evals/test_verifier_port.py`'s assertions are covered, deletes it and the rest of `tests/evals/`, and removes the `tests/evals` `t2` marking from `tests/conftest.py` (added in P0A-05)
- **Notes:** positive and negative pairs share one case ID; list both files in the Test cell. Exit criterion: the confusion matrix is at 1.0.

### P1-13 — `SCHED` (T2-SCHED-01 … T2-SCHED-10)

- **Depends on:** P1-02, P1-03 · **Blocked by:** none
- **Read:** [6L-sched.md](../reference/06-coverage/6L-sched.md), the T2 rows without a `G` suffix only
- **Creates:** `regression/t2/loop/scenarios/scheduler/*.yaml` (01–04, 09, 10), `regression/t2/python/test_scheduler_engine.py` (05–08)
- **Notes:** the `T2-SCHED-G*` gateway cases are P2-04.

### P1-14 — `MCP` (T2-MCP-01 … T2-MCP-11)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6N-mcp.md](../reference/06-coverage/6N-mcp.md)
- **Creates:** `regression/t2/loop/scenarios/mcp/*.yaml`, `regression/fixtures/configs/mcp.*.json`
- **Notes:**
  - The fixture server runs as a real subprocess; there is no MCP mock here.
  - Do not move `evals/smoke_agent/fixture_mcp_server.py`. The live agent's `mcp.json` and `tests/unit/core/mcp/test_mcp_client.py` point at it, and P4-02 moves it with the agent. Until then, the T2 MCP configs reference it at that path; write the path into the configs in one place (for example a `{repo_root}` substitution in `drivers/loop.py`) so P4-02 changes one line.

### P1-15 — `MEM` (T2-MEM-01 … T2-MEM-09)

- **Depends on:** P1-03, P1-11 (T2-MEM-07 spawns a child) · **Blocked by:** none
- **Read:** [6P-mem.md](../reference/06-coverage/6P-mem.md)
- **Creates:** `regression/t2/loop/scenarios/memory/*.yaml`, `regression/t2/python/test_memory_commit.py` (MEM-03)
- **Notes:** uses `testkit.palace.InMemoryPalace` and real outbox rows. MEM-06 (from `tool_search_memory`) and MEM-08 were migrated in P0B-05; extend them to their rows' asserts, do not duplicate them.

### P1-16 — `SKILL` and `TODO` (T2-SKILL-01 … 06, T2-TODO-02 … 06)

- **Depends on:** P1-01, P1-03 · **Blocked by:** none
- **Read:** [6M-skill.md](../reference/06-coverage/6M-skill.md); [6S-todo.md](../reference/06-coverage/6S-todo.md)
- **Creates:** `regression/t2/loop/scenarios/skills/*.yaml`, `regression/t2/loop/scenarios/todo/*.yaml`

### P1-17 — `ATT`, `WEB`, `COMP` (T2-ATT-02 … 07, T2-WEB-02 … 05, T2-COMP-01 … 06)

- **Depends on:** P1-01, P1-03, P1-14 (T2-WEB-05 uses an MCP server named `browser`) · **Blocked by:** none (`COMP` runs only on macOS, D4)
- **Read:** [6H-att.md](../reference/06-coverage/6H-att.md) (non-`G` T2 rows); [6O-web.md](../reference/06-coverage/6O-web.md); [6I-comp.md](../reference/06-coverage/6I-comp.md)
- **Creates:** `testkit/web_search.py`, `regression/t2/loop/scenarios/{attachments,web,computer}/*.yaml`
- **Notes:**
  - Tag the `COMP` scenarios `darwin-only` in YAML. `regression/plugin.py` maps that tag to the `darwin_only` marker and `regression/conftest.py` skips those items when `sys.platform != "darwin"` (§5.2). Add the mapping and the skip here if they do not exist yet.
  - Switch the migrated T2-WEB-01 to the fake backend.

### P1-18 — `STORE` and `OPS` at T2 (T2-STORE-01, 02; T2-OPS-01, 02, 04 … 06); phase exit

- **Depends on:** P1-04 … P1-17 · **Blocked by:** none
- **Read:** [6Q-store.md](../reference/06-coverage/6Q-store.md), the non-`G` T2 rows only; [6W-ops.md](../reference/06-coverage/6W-ops.md), the T2 rows only; this phase file's Summary
- **Creates:** `regression/t2/loop/scenarios/store/{compaction_reset_row_ids,usage_recorded_on_turn_complete}.yaml`, `regression/t2/python/test_ops_cli.py` (OPS-01, 02), `regression/t2/python/test_ops_observability.py` (OPS-04..06)
- **Notes:** T2-STORE-03 needs `from_transcript.py` and belongs to P3-01; T2-OPS-03 needs the in-process gateway and belongs to P2-04.
- **Steps:**
  1. Write the cases.
  2. Check the Summary exit criteria. `rg -n 'tests/evals' tests regression` should find nothing.
- **Done when:** the area "Done when" holds for STORE and OPS, and the phase Summary exit criteria hold.
