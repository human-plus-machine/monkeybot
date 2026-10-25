# Tasks

> Part of the [regression plan](README.md). How to pick and run a task: [AGENT_GUIDE.md](AGENT_GUIDE.md). Each task's full instructions are in its phase file (last column). Progress is tracked in [status.md](status.md); this file does not change while tasks are worked on.

Dependencies, not phase numbers, decide what can start: a task can begin when everything in **Depends on** is `done` and nothing in **Blocked by** is open. The phase exit tasks (P0A-06, P0B-07, P1-18, P4-06, P6-01) check that a phase is green. Sizes in the phase files are rough estimates for one person.

| ID | Task | Phase | Depends on | Blocked by | File |
|---|---|---|---|---|---|
| P0A-01 | Failure ledger: plugin, scrubber, triage CLI | 0a | none | none | [phase-0a](phases/phase-0a-tier1-reorg.md) |
| P0A-02 | Agent rule and Cursor hook | 0a | P0A-01 | none | [phase-0a](phases/phase-0a-tier1-reorg.md) |
| P0A-03 | Shared helpers into `testkit` | 0a | P0A-01 | none | [phase-0a](phases/phase-0a-tier1-reorg.md) |
| P0A-04 | `git mv` tier-1 tests into `tests/unit/` | 0a | P0A-03 | none | [phase-0a](phases/phase-0a-tier1-reorg.md) |
| P0A-05 | Integration and acceptance tests into `regression/`; config and scripts | 0a | P0A-04 | none | [phase-0a](phases/phase-0a-tier1-reorg.md) |
| P0A-06 | Phase 0a exit check and cleanup | 0a | P0A-02, P0A-05 | none | [phase-0a](phases/phase-0a-tier1-reorg.md) |
| P0B-01 | Multi-turn strict `ScriptedProvider` | 0b | P0A-06 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P0B-02 | Scenario schema (models) | 0b | P0A-06 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P0B-03 | Assertion engine and invariants | 0b | P0B-02 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P0B-04 | Loop driver, record hook, YAML collector, `regression/plugin.py` and `conftest.py` | 0b | P0B-01, P0B-03 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P0B-05 | Migrate the 19 `tests/evals` scenarios | 0b | P0B-04 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P0B-06 | `docs/testing.md`, catalog parser, pairing test | 0b | P0B-05 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P0B-07 | Wire `regression/` into the gates; phase exit | 0b | P0B-06 | none | [phase-0b](phases/phase-0b-foundations.md) |
| P1-01 | New fixtures | 1 | P0B-07 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-02 | `FakeClock` and the `now_ms` seam | 1 | P0B-07 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-03 | `steps:` execution and `PresetPendingBus` | 1 | P1-02 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-04 | `LOOP` T2 cases | 1 | P1-03, P1-14 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-05 | `FILE` T2 cases (except 08 and 17) | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-06 | `SHELL` T2 cases | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-07 | `POL` T2 cases | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-08 | `CODE` T2 cases | 1 | P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-09 | `HOOK` T2 cases | 1 | P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-10 | `CTX` T2 cases | 1 | P1-01, P1-02, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-11 | `SUB` T2 cases, fake worker, tapes | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-12 | `VER` T2 cases; retire `test_verifier_port.py` | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-13 | `SCHED` T2 cases | 1 | P1-02, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-14 | `MCP` T2 cases | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-15 | `MEM` T2 cases | 1 | P1-03, P1-11 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-16 | `SKILL` and `TODO` T2 cases | 1 | P1-01, P1-03 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-17 | `ATT`, `WEB`, `COMP` T2 cases | 1 | P1-01, P1-03, P1-14 | none (`COMP` runs on macOS only, D4) | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P1-18 | `STORE` and `OPS` T2 cases (not STORE-03, OPS-03); phase exit | 1 | P1-04 … P1-17 | none | [phase-1](phases/phase-1-t2-harness-logic.md) |
| P2-01 | Gateway harness, SSE reader, gateway driver | 2 | P0B-07 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-02 | SSE schema contract and golden streams | 2 | P2-01 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-03 | Route table and error envelope | 2 | P2-01 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-04 | Remaining gateway cases, plus T2-OPS-03 | 2 | P2-02 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-05 | Realtime driver and cases | 2 | P2-01 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-06 | Provider conformance | 2 | P1-18 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-07 | Storage parity and migrations | 2 | P1-18 | D3 (Postgres / Firestore part only) | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-08 | Cross-cutting T1 contracts | 2 | P1-18 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-09 | Single-package T1 additions | 2 | P1-18 | none | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P2-10 | monkeyapp contract (cross-repo) | 2 | P2-02 | user confirmation to edit monkeyapp | [phase-2](phases/phase-2-contracts-and-gateway.md) |
| P3-01 | Cassette model, `from_transcript`, replay driver, T2-STORE-03 | 3 | P0B-07 | none | [phase-3](phases/phase-3-record-replay.md) |
| P3-02 | Record the five T3a cassettes (live) | 3 | P3-01, P4-03 | user confirmation (live) | [phase-3](phases/phase-3-record-replay.md) |
| P3-03 | Wire cassettes and staleness check | 3 | P3-01, P4-02 | user confirmation (live); D13 for non-Ollama providers | [phase-3](phases/phase-3-record-replay.md) |
| P3-04 | Golden SSE from a cassette; MCP, subagent, wake-up replays | 3 | P3-01, P2-02 | none | [phase-3](phases/phase-3-record-replay.md) |
| P4-01 | Move `evals/` code into `regression/t4/` and the selftests | 4 | P0B-07 | none | [phase-4](phases/phase-4-live.md) |
| P4-02 | Agent project, MCP fixture move, config split, judge config, preflight, ledger records | 4 | P4-01 | D12 (judge model value only) | [phase-4](phases/phase-4-live.md) |
| P4-03 | Convert the 29 live scenarios and suites | 4 | P4-02 | none | [phase-4](phases/phase-4-live.md) |
| P4-04 | New T4 cases and suites | 4 | P4-03 | none (T4-RT-01 deferred) | [phase-4](phases/phase-4-live.md) |
| P4-05 | Live workflow and artifacts | 4 | P4-03 | D1 (running in CI only) | [phase-4](phases/phase-4-live.md) |
| P4-06 | Live smoke; phase exit | 4 | P4-04, P4-05 | user confirmation (live); D1 for the CI part | [phase-4](phases/phase-4-live.md) |
| P5-01 | Statistics, comparison, class lists | 5 | P4-06 | none | [phase-5](phases/phase-5-trials.md) |
| P5-02 | First baselines (live, hours) | 5 | P5-01 | user confirmation (live); D12 for judged suites | [phase-5](phases/phase-5-trials.md) |
| P5-03 | Nightly workflow, release-gate compare, gateway storms | 5 | P5-02, P2-01 | D1 (running in CI) | [phase-5](phases/phase-5-trials.md) |
| P6-01 | Cutover | 6 | every P1–P3 task done or deferred; P5-03 (or P4-06 with Phase 5 deferred) | none | [phase-6](phases/phase-6-cutover.md) |

## Open decisions that block tasks

Full text is in [12-decisions.md](reference/12-decisions.md). Only the user resolves these.

| Decision | Blocks |
|---|---|
| D1: add `OLLAMA_API_KEY` as a GitHub secret? | CI runs in P4-05, P4-06, P5-03 |
| D3: Postgres / Firestore in CI | the non-sqlite part of P2-07 |
| D4: macOS runner for `COMP` | CI runs of the P1-17 `COMP` cases (local runs proceed) |
| D12: judge model on Ollama Cloud | the `judge_model` value in P4-02; judged suites in P5-02 |
| D13: wire cassettes for non-Ollama providers | T3-PROV-01..05 and the non-Ollama families of T3-PROV-06, in P3-03 |
