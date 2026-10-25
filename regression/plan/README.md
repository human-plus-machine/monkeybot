# monkeybot regression revamp — plan

Status: draft, 2026-10-25. Owner: kz127.

This plan replaces `evals/` and `tests/evals/` with one tiered regression system and reorganizes the existing unit tests into tier 1. The repo ends up with three test roots:

- `tests/` at the repo root is **tier 1 only**: every existing unit test, re-homed so the folders mirror `src/monkeybot/`, plus the new cross-cutting contract tests.
- `regression/` at the repo root holds **tiers 2–5**: scripted trajectories, record/replay, live evals, trials.
- `testkit/` at the repo root is the **shared test-double package** both of them import. It replaces the 66 imports (in 45 files) where one test module imports helpers from another test module today (`from tests.core.test_loop import FakeProvider, ...`).

Three cross-cutting pieces apply to every tier:

- A **failure ledger** (§5.9): every failing test, scenario, or trial, from any tier, is appended to a run file in `test-runs/`, and agents are told to report those failures instead of fixing them.
- A **test catalog document**, `docs/testing.md` (§5.10), laid out like monkeyapp's `docs/testing.md`: tiers, gates, authoring rules, failure policy, and one catalog row per case, kept in sync with the tests by a tier-1 pairing test.
- **Ollama Cloud for every live run** (§5.11), using the owner's local `OLLAMA_API_KEY`.

`cli/tests/` and `integrations/browser-mcp/tests/` stay where they are; they belong to separately packaged projects with their own `pyproject.toml`.

Case IDs look like `T2-LOOP-04`. The prefix is the tier, the middle is the feature area from §6, the number is stable once assigned (do not renumber; retire instead). Task IDs look like `P1-04`: phase, then order within the phase.

## If you are an agent

Read four files, in this order, and nothing else until your task asks for it:

1. [AGENT_GUIDE.md](AGENT_GUIDE.md): how to pick a task, what you may and may not change, when to stop, how to report.
2. [tasks.md](tasks.md): every task in order with its dependencies and blockers. It does not change while work is in progress.
3. [status.md](status.md): what is done, in progress or blocked, the open decisions, and the handoff log. This is the only plan file agents edit.
4. The phase file for your task in [`phases/`](phases/). Each task there lists the exact reference files (and sections) to read.

An agent assigned "P0A-01 to P0A-04" reads the guide, `tasks.md`, `status.md`, `phases/phase-0a-tier1-reorg.md`, and the handful of reference files those four tasks list. It never needs the coverage matrix, the live-tier design, or the trials statistics.

## If you are a person

Start with [reference/01-goals-and-principles.md](reference/01-goals-and-principles.md) and [reference/03-tiers.md](reference/03-tiers.md), then [reference/04-file-structure.md](reference/04-file-structure.md) for what the repo looks like afterwards. Open decisions are in [reference/12-decisions.md](reference/12-decisions.md).

## Layout

```text
regression/plan/
  README.md                       # this index
  AGENT_GUIDE.md                  # §13: rules and workflow for agents
  tasks.md                        # ordered task table: depends on, blocked by (static)
  status.md                       # live progress: task and phase status, decisions, handoff log
  phases/
    phase-0a-tier1-reorg.md       # §8 Phase 0a + tasks P0A-*
    phase-0b-foundations.md       # §8 Phase 0b + tasks P0B-*
    phase-1-t2-harness-logic.md   # §8 Phase 1  + tasks P1-*
    phase-2-contracts-and-gateway.md
    phase-3-record-replay.md
    phase-4-live.md
    phase-5-trials.md
    phase-6-cutover.md
  reference/
    01-goals-and-principles.md    # §1, §2 (assumptions A1–A12, what is unverified)
    03-tiers.md                   # §3
    04-file-structure.md          # §4 (every folder and file, old → new paths)
    05.1-doubles.md               # §5.1
    05.2-scenario-schema.md       # §5.2
    05.3-assertions-and-invariants.md  # §5.3
    05.4-fixtures.md              # §5.4
    05.5-cassettes.md             # §5.5
    05.6-pytest-reporting-trials.md    # §5.6, §5.7, §5.8
    05.9-failure-ledger.md        # §5.9
    05.10-testing-doc.md          # §5.10
    05.11-live-provider.md        # §5.11
    06-coverage/
      00-index.md                 # §6 intro
      6A-loop.md … 6W-ops.md      # §6.A–§6.W, one feature area per file
    07-migration-map.md           # §7
    09-ci.md                      # §9
    10-policies.md                # §10
    11-known-issues.md            # §11
    12-decisions.md               # §12 (D1–D13)
    appendices.md                 # Appendix A–D
```

## Section map

| Reference | File |
|---|---|
| §1 Goals, §2 First principles (A1–A12, 2.3 unverified) | [reference/01-goals-and-principles.md](reference/01-goals-and-principles.md) |
| §3 Tiers | [reference/03-tiers.md](reference/03-tiers.md) |
| §4.1–§4.6 File structure | [reference/04-file-structure.md](reference/04-file-structure.md) |
| §5.1 Doubles | [reference/05.1-doubles.md](reference/05.1-doubles.md) |
| §5.2 Scenario schema | [reference/05.2-scenario-schema.md](reference/05.2-scenario-schema.md) |
| §5.3 Assertions, invariants | [reference/05.3-assertions-and-invariants.md](reference/05.3-assertions-and-invariants.md) |
| §5.4 Fixtures | [reference/05.4-fixtures.md](reference/05.4-fixtures.md) |
| §5.5 Cassettes | [reference/05.5-cassettes.md](reference/05.5-cassettes.md) |
| §5.6 Pytest wiring, §5.7 Reporting, §5.8 T5 statistics | [reference/05.6-pytest-reporting-trials.md](reference/05.6-pytest-reporting-trials.md) |
| §5.9 Failure ledger | [reference/05.9-failure-ledger.md](reference/05.9-failure-ledger.md) |
| §5.10 `docs/testing.md` | [reference/05.10-testing-doc.md](reference/05.10-testing-doc.md) |
| §5.11 Live provider (Ollama Cloud) | [reference/05.11-live-provider.md](reference/05.11-live-provider.md) |
| §6 intro | [reference/06-coverage/00-index.md](reference/06-coverage/00-index.md) |
| §6.A `LOOP` | [6A-loop.md](reference/06-coverage/6A-loop.md) |
| §6.B `CTX` | [6B-ctx.md](reference/06-coverage/6B-ctx.md) |
| §6.C `HOOK` | [6C-hook.md](reference/06-coverage/6C-hook.md) |
| §6.D `FILE` | [6D-file.md](reference/06-coverage/6D-file.md) |
| §6.E `SHELL` | [6E-shell.md](reference/06-coverage/6E-shell.md) |
| §6.F `POL` | [6F-pol.md](reference/06-coverage/6F-pol.md) |
| §6.G `CODE` | [6G-code.md](reference/06-coverage/6G-code.md) |
| §6.H `ATT` | [6H-att.md](reference/06-coverage/6H-att.md) |
| §6.I `COMP` | [6I-comp.md](reference/06-coverage/6I-comp.md) |
| §6.J `SUB` | [6J-sub.md](reference/06-coverage/6J-sub.md) |
| §6.K `VER` | [6K-ver.md](reference/06-coverage/6K-ver.md) |
| §6.L `SCHED` | [6L-sched.md](reference/06-coverage/6L-sched.md) |
| §6.M `SKILL` | [6M-skill.md](reference/06-coverage/6M-skill.md) |
| §6.N `MCP` | [6N-mcp.md](reference/06-coverage/6N-mcp.md) |
| §6.O `WEB` | [6O-web.md](reference/06-coverage/6O-web.md) |
| §6.P `MEM` | [6P-mem.md](reference/06-coverage/6P-mem.md) |
| §6.Q `STORE` | [6Q-store.md](reference/06-coverage/6Q-store.md) |
| §6.R `CFG` | [6R-cfg.md](reference/06-coverage/6R-cfg.md) |
| §6.S `TODO` | [6S-todo.md](reference/06-coverage/6S-todo.md) |
| §6.T `PROV` | [6T-prov.md](reference/06-coverage/6T-prov.md) |
| §6.U `GW` | [6U-gw.md](reference/06-coverage/6U-gw.md) |
| §6.V `RT` | [6V-rt.md](reference/06-coverage/6V-rt.md) |
| §6.W `OPS` | [6W-ops.md](reference/06-coverage/6W-ops.md) |
| §7 Migration map | [reference/07-migration-map.md](reference/07-migration-map.md) |
| §8 Roadmap (Phase 0a–6) | [`phases/`](phases/) (each phase file opens with its §8 summary) |
| §9 CI matrix | [reference/09-ci.md](reference/09-ci.md) |
| §10 Policies | [reference/10-policies.md](reference/10-policies.md) |
| §11 Known issues | [reference/11-known-issues.md](reference/11-known-issues.md) |
| §12 Decisions D1–D13 | [reference/12-decisions.md](reference/12-decisions.md) |
| §13 Agent guide | [AGENT_GUIDE.md](AGENT_GUIDE.md) |
| Appendix A–D | [reference/appendices.md](reference/appendices.md) |
| A case ID such as `T2-LOOP-07` | the §6 area file for its middle part (`LOOP` → `6A-loop.md`) |

## Keeping the plan consistent

- Section numbers stay in the file headings, so §-references keep working after the split. Do not renumber.
- Change a fact in one file only. If a phase file and a reference file disagree, the reference file wins; fix the phase file.
- Progress (task and phase status, handoff notes) lives only in `status.md`. `tasks.md`, the phase files and the reference files change only when the plan itself changes.
- After Phase 6, `docs/testing.md` is the living source for what is tested where; this plan becomes history.
