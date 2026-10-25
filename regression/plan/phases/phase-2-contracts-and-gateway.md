# Phase 2 — Contracts at T1 and the gateway driver (≈ 1 week)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

- `testkit/gateway.py` + `testkit/sse.py` (streaming SSE reader) and `regression/drivers/gateway.py`.
- `T1-GW-*` in `tests/contracts/`, `tests/contracts/goldens/sse/schema.json`, `regression/t3/sse/*.sse`, `T2-GW-*`, `T2-*-G*` gateway cases, `T2-RT-*`.
- `T1-PROV-*` conformance; `T1-STORE-*` parity (sqlite always; Postgres/Firestore in an optional job); `T1-CFG-*`; `T1-CTX-01` prompt golden; `T1-FILE-01/02`; `T1-POL-01`.
- monkeyapp: vendor schema + `T3-GW-02/03` (separate PR in that repo).

Exit: schema generated and validated on both sides; route table snapshot committed; parity matrix green on sqlite and on whichever of Postgres/Firestore D3 enables; their catalog rows have Test paths.

## Tasks

Dependencies decide the order (tasks.md):

- P2-01 first among the gateway tasks. After it: P2-02, P2-03 and P2-05 in any order; P2-04 and P2-10 after P2-02.
- P2-06, P2-07, P2-08 and P2-09 do not need the gateway. They start once P1-18 is done.

Tier-1 tasks here follow the same case loop as Phase 1 (read the target code, write the test, decide whether a failure is in the test or in the harness, fill the catalog row). A harness discrepancy is handled exactly as Phase 1's area procedure, step 3: leave it failing, report it, set the task `blocked` for the user's ruling. Contract tests go under `tests/contracts/`; single-package tests go under `tests/unit/<package>/` (§4.3 placement rule).

Every task's Verify also includes `uv run pytest tests/contracts/test_catalog_pairing.py -q` and `uv run pytest tests -q -n auto` (no new failures outside the task's own files). `--update-golden` is registered once by `regression/plugin.py` (P0B-04) and works under `tests/` too; never register it in a conftest.

---

### P2-01 — Gateway harness, streaming SSE reader, gateway driver

- **Depends on:** P0B-07
- **Blocked by:** none
- **Read:**
  - [05.1-doubles.md](../reference/05.1-doubles.md), the gateway-harness row only;
  - [6U-gw.md](../reference/06-coverage/6U-gw.md), intro and "Contract plan" only;
  - [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `fixtures/scripts/` and `t2/gateway/` lines only;
  - `src/monkeybot/gateway/sse/app.py` (`create_app`, lifespan) and `sse.py`.
- **Creates / changes:**
  - `testkit/sse.py`, `testkit/gateway.py`, `regression/drivers/gateway.py`
  - `regression/fixtures/scripts/` (provider scripts the gateway reads through `MONKEYBOT_FAKE_PROVIDER_SCRIPT`)
  - `steps:` `disconnect_sse` / `reconnect_sse(last_event_id)`
  - `tests/unit/testkit/test_sse_reader.py`
  - (`asgi-lifespan` is already a dev dependency.)
- **Steps:**
  1. The reader parses `id:`, `data:` and comment frames from a streaming response, and stops at a predicate. Today's `ASGITransport` buffers the whole response, so this needs a real streaming path; prove it in a test.
  2. The harness builds `create_app` with lifespan, a temp agent root, and `MODEL_PROVIDER=fake` with `MONKEYBOT_FAKE_PROVIDER_SCRIPT`.
  3. The driver runs `driver: gateway` scenarios: POST `/reply`, read events, apply the SSE steps.
- **Verify:** `uv run pytest tests/unit/testkit/test_sse_reader.py regression/selftest -q`. A one-turn gateway scenario passes with invariants 5 and 8 enabled.
- **Done when:** gateway scenarios run in-process, with no network ports.
- **Stop and ask if:** a streaming read is impossible in-process without binding a real port. Port 0 is acceptable; ask before anything else.

### P2-02 — SSE schema contract and golden streams (T1-GW-01 … 04, T2-GW-01, 02)

- **Depends on:** P2-01
- **Blocked by:** none
- **Read:** [6U-gw.md](../reference/06-coverage/6U-gw.md), "Contract plan" items 1, 2 and 5, and rows T1-GW-01..04, T2-GW-01..02; [05.5-cassettes.md](../reference/05.5-cassettes.md) §5.5.3; [appendices.md](../reference/appendices.md) Appendix B
- **Creates / changes:** `tests/contracts/test_sse_schema.py`, `tests/contracts/goldens/sse/schema.json`, `regression/t3/sse/{basic_turn,tool_turn}.sse`, `regression/t2/gateway/scenarios/gw/{scripted_turn_golden_sse,tool_turn_golden_sse}.yaml`; invariant 6 turned on in `regression/schema/invariants.py`; `jsonschema` added with `uv add --dev jsonschema` (today it is only a transitive dependency; D6 assumed); the tests that use `agent_event_to_wire_dict` switch to `event_to_json` (Contract plan item 5; tests only, `src/` unchanged)
- **Steps:**
  1. Generate the schema with `--update-golden` from `event_to_json` over every `AgentEvent` variant.
  2. Write the goldens using the §5.5.3 normalization (durations, trace ids, timestamps, ids).
  3. Show the generated `schema.json` and both `.sse` files in the report; goldens are reviewed like code.
- **Verify:** `uv run pytest tests/contracts/test_sse_schema.py -q`; `uv run pytest regression/t2/gateway/scenarios/gw -q`
- **Done when:** the schema validates every production event, and the goldens compare byte-for-byte after normalization.
- **Stop and ask if:** removing a test's use of `agent_event_to_wire_dict` would need a change in `src/`.

### P2-03 — Route table and error envelope (T1-GW-05, 06)

- **Depends on:** P2-01 · **Blocked by:** none
- **Read:** [6U-gw.md](../reference/06-coverage/6U-gw.md), rows T1-GW-05, 06; [appendices.md](../reference/appendices.md) Appendix D
- **Creates:** `tests/contracts/test_route_table.py`, `tests/contracts/goldens/routes.txt` (generated with `--update-golden`, shown in the report)
- **Verify:** `uv run pytest tests/contracts/test_route_table.py -q`
- **Done when:** both cases pass and the golden matches Appendix D.

### P2-04 — Remaining gateway cases (T2-GW-03 … 21; T2-ATT-G1, G2; T2-SCHED-G1, G2; T2-STORE-G1; T2-CFG-G1 … G5; T2-OPS-03)

- **Depends on:** P2-02 · **Blocked by:** none
- **Read:** [6U-gw.md](../reference/06-coverage/6U-gw.md), T2 rows; the `G` rows only of [6H-att.md](../reference/06-coverage/6H-att.md), [6L-sched.md](../reference/06-coverage/6L-sched.md), [6Q-store.md](../reference/06-coverage/6Q-store.md); [6R-cfg.md](../reference/06-coverage/6R-cfg.md), T2 rows; [6W-ops.md](../reference/06-coverage/6W-ops.md), row T2-OPS-03; [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t2/gateway/` subtree
- **Creates:** `regression/t2/gateway/scenarios/{gw,attachments,scheduler,store,config}/*.yaml`, `regression/t2/python/test_sse_stream_pressure.py` (GW-06, 07, 21), T2-OPS-03 added to `regression/t2/python/test_ops_cli.py`
- **Verify:** `uv run pytest regression/t2/gateway regression/t2/python/test_sse_stream_pressure.py regression/t2/python/test_ops_cli.py -q -n auto`
- **Done when:** every listed case exists with its Test cell, and passes or is a reported discrepancy.

### P2-05 — Realtime driver (T2-RT-01 … 07, T1-RT-01)

- **Depends on:** P2-01 · **Blocked by:** none
- **Read:** [6V-rt.md](../reference/06-coverage/6V-rt.md); [05.2-scenario-schema.md](../reference/05.2-scenario-schema.md), the `driver` and `steps` rows; [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t2/realtime/` lines; `src/monkeybot/core/testing/mocks_realtime_provider.py`
- **Creates / changes:** `regression/drivers/realtime.py`; `regression/schema/{scenario,steps}.py` (`driver: realtime`; `client_text` and `interrupt` steps); `regression/t2/realtime/scenarios/*.yaml`; `tests/contracts/test_realtime_divergence.py`
- **Notes:** uses `mocks_realtime_provider`; no GCP credentials. T4-RT-01 stays deferred (§5.11).
- **Verify:** `uv run pytest regression/t2/realtime tests/contracts/test_realtime_divergence.py regression/selftest -q`
- **Done when:** all eight cases exist with Test cells and pass or are reported discrepancies.

### P2-06 — Provider conformance (T1-PROV-01 … 10)

- **Depends on:** P1-18 · **Blocked by:** none
- **Read:** [6T-prov.md](../reference/06-coverage/6T-prov.md), T1 rows; `src/monkeybot/providers/` for each adapter as you reach it
- **Creates:** `tests/contracts/test_provider_conformance.py`, `tests/contracts/tables/provider_error_mapping.yaml`
- **Notes:** parametrize over adapters. The canonical conversation comes from `testkit.conversations`. No network.
- **Verify:** `uv run pytest tests/contracts/test_provider_conformance.py -q -n auto`
- **Done when:** every adapter × case cell passes or is a reported discrepancy.

### P2-07 — Storage parity and migrations (T1-STORE-01 … 14, 20 … 25)

- **Depends on:** P1-18
- **Blocked by:** D3, for the Postgres/Firestore runs only. sqlite proceeds without it.
- **Read:** [6Q-store.md](../reference/06-coverage/6Q-store.md), T1 rows; [05.1-doubles.md](../reference/05.1-doubles.md), the `open_backend` row; [04-file-structure.md](../reference/04-file-structure.md) §4.3, the `tests/contracts/` subtree
- **Creates:** `testkit/backends.py`, the `open_backend` fixture in `tests/contracts/conftest.py`, `tests/contracts/test_storage_parity.py`, `tests/contracts/test_migrations_replay.py`, `tests/contracts/fixtures/legacy_db/{pre_row_id,pre_agent_scope,pre_branch_pk,pre_usage_cache_cols}.sqlite`
- **Steps:**
  1. Build each legacy DB from the schema of the commit that last had that layout (`git log -S` on the column, then `git show <sha>:<schema file>`). Record the SHA in the test's docstring.
  2. Postgres/Firestore parameters carry the `needs_postgres` / `needs_firestore` markers and skip with the shared message unless `MONKEYBOT_TEST_POSTGRES_URL` / `FIRESTORE_EMULATOR_HOST` are set.
  3. The optional `storage-parity` CI job (§9) waits for D3; do not add it here.
- **Verify:** `uv run pytest tests/contracts/test_storage_parity.py tests/contracts/test_migrations_replay.py -q`
- **Done when:** the sqlite parameters pass (or are reported discrepancies), and the other backends skip with the shared message.

### P2-08 — Cross-cutting T1 contracts (T1-CFG-01 … 06, T1-CTX-01, 02, T1-FILE-01, 02, T1-POL-01 … 03, T1-OPS-01, 02)

- **Depends on:** P1-18 · **Blocked by:** none
- **Read:** the T1 rows only of [6R-cfg.md](../reference/06-coverage/6R-cfg.md), [6B-ctx.md](../reference/06-coverage/6B-ctx.md), [6D-file.md](../reference/06-coverage/6D-file.md), [6F-pol.md](../reference/06-coverage/6F-pol.md), [6W-ops.md](../reference/06-coverage/6W-ops.md); [appendices.md](../reference/appendices.md) Appendix A; [04-file-structure.md](../reference/04-file-structure.md) §4.3, the `tests/contracts/` subtree
- **Creates:**
  - `tests/contracts/test_config_matrix.py`, `test_harness_prompt_golden.py`, `test_tool_error_catalog.py`, `test_policy_decision_table.py`, `test_scaffold_reexports.py`, `test_span_names.py`
  - `tests/contracts/tables/{policy_decisions,config_precedence}.yaml`
  - goldens `prompts/*`, `tool_error_codes.txt`, `span_names.txt`
- **Notes:** goldens are generated with `--update-golden` and shown in the report.
- **Verify:** `uv run pytest tests/contracts -q -n auto`
- **Done when:** every listed case exists with its Test cell, and passes or is a reported discrepancy.

### P2-09 — Single-package T1 additions (T1-SKILL-01, T1-MEM-01 … 03, T1-SUB-01, T1-SCHED-01, T1-COMP-01)

- **Depends on:** P1-18 · **Blocked by:** none
- **Read:** the T1 rows only of [6M-skill.md](../reference/06-coverage/6M-skill.md), [6P-mem.md](../reference/06-coverage/6P-mem.md), [6J-sub.md](../reference/06-coverage/6J-sub.md), [6L-sched.md](../reference/06-coverage/6L-sched.md), [6I-comp.md](../reference/06-coverage/6I-comp.md)
- **Creates:**
  - `tests/unit/core/context/test_skill_discovery_rules.py`
  - `tests/unit/core/memory/test_memory_scoping.py`
  - `tests/unit/core/subagents/test_child_tool_list.py`
  - `tests/unit/computer/test_platform_gate.py`
  - extends `tests/unit/scheduler/test_interval.py` (new test functions only; existing ones unchanged)
- **Verify:** `uv run pytest tests/unit/core/context/test_skill_discovery_rules.py tests/unit/core/memory/test_memory_scoping.py tests/unit/core/subagents/test_child_tool_list.py tests/unit/computer/test_platform_gate.py tests/unit/scheduler/test_interval.py -q`
- **Done when:** every listed case exists with its Test cell, and passes or is a reported discrepancy.

### P2-10 — monkeyapp contract (T3-GW-02, 03), cross-repo

- **Depends on:** P2-02
- **Blocked by:** user confirmation to edit monkeyapp (AGENT_GUIDE §13.5)
- **Read:** [6U-gw.md](../reference/06-coverage/6U-gw.md), "Contract plan" items 3–4 and rows T3-GW-02, 03; `/Users/kz127/code/monkeyapp/docs/testing.md` §1–§2; `monkeyapp/tests/harness/fake-gateway.ts`
- **Creates (in monkeyapp):** a vendoring script that copies `schema.json` with the monkeybot commit in its header, and two L1 tests (fake-gateway frames validate; dispatcher cases name schema types)
- **Creates (in monkeybot):** pairing-test support for Test cells written `monkeyapp:<path>`. The pairing test checks such a path only when `../monkeyapp` exists, and otherwise skips it with a reason.
- **Notes:**
  - The drift listed in 6.U (missing `usage`, `label`, `call_id`) will make these tests fail. In monkeyapp that is recorded, not fixed by this task; fixing the fake gateway is a separate monkeyapp task.
  - Follow monkeyapp's own `docs/testing.md` rules there.
- **Verify:** monkeyapp's own L1 command for the two new tests (per its `docs/testing.md`); `uv run pytest tests/contracts/test_catalog_pairing.py -q` in monkeybot.
- **Done when:** both tests exist and run in monkeyapp (failing on the known drift is expected and reported), and the two catalog rows have `monkeyapp:` Test cells.
