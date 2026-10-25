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
- **Last update:** 2026-10-25, P0A-01 done
- **Next task:** P0A-02

## Phases

| Phase | Name | Tasks | Done | Status | Exit check | Completed |
|---|---|---|---|---|---|---|
| [0a](phases/phase-0a-tier1-reorg.md) | Tier-1 reorganization and failure ledger | 6 | 1 | in-progress | P0A-06 | |
| [0b](phases/phase-0b-foundations.md) | Foundations and docs/testing.md | 7 | 0 | todo | P0B-07 | |
| [1](phases/phase-1-t2-harness-logic.md) | T2 harness logic | 18 | 0 | todo | P1-18 | |
| [2](phases/phase-2-contracts-and-gateway.md) | Contracts and gateway | 10 | 0 | todo | all P2 tasks done | |
| [3](phases/phase-3-record-replay.md) | Record / replay | 4 | 0 | todo | all P3 tasks done | |
| [4](phases/phase-4-live.md) | Live tier | 6 | 0 | todo | P4-06 | |
| [5](phases/phase-5-trials.md) | Trials and baselines | 3 | 0 | todo | P5-03 | |
| [6](phases/phase-6-cutover.md) | Cutover | 1 | 0 | todo | P6-01 | |

## Tasks

| ID | Status | Owner | Updated | Ref | Note |
|---|---|---|---|---|---|
| P0A-01 | done | cursor | 2026-10-25 | uncommitted | baseline 3320 collected, 3295 passed, 25 skipped |
| P0A-02 | todo | | | | |
| P0A-03 | todo | | | | |
| P0A-04 | todo | | | | |
| P0A-05 | todo | | | | |
| P0A-06 | todo | | | | |
| P0B-01 | todo | | | | |
| P0B-02 | todo | | | | |
| P0B-03 | todo | | | | |
| P0B-04 | todo | | | | |
| P0B-05 | todo | | | | |
| P0B-06 | todo | | | | |
| P0B-07 | todo | | | | |
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

