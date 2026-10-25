# 13. Implementation guide for agents

> Part of the [regression plan](README.md). Read this file, [tasks.md](tasks.md), [status.md](status.md), and the one phase file for your task. Do not read the rest of the plan unless your task's **Read** list names it.

This guide is how an agent builds the regression system one task at a time, with only the context that task needs. Every task lives in a phase file under [`phases/`](phases/) and has the same shape: depends on, blocked by, read, creates/changes, steps, verify, done when, stop and ask if.

## 13.1 Picking a task

1. Open [tasks.md](tasks.md) (what each task is and depends on) and [status.md](status.md) (what is done). If the user named a task or a range ("do P1-01 to P1-04"), do exactly those, in order. Otherwise ask which task to do; do not pick one yourself.
2. Check the task's **Depends on**: every listed task must be `done` in `status.md`. If one is not, stop and say which.
3. Check **Blocked by**. If it names an open decision (the Decisions table in `status.md`; full text in [reference/12-decisions.md](reference/12-decisions.md)) and the decision still blocks the part of the task you would do next, stop and ask the user that one question. A decision that blocks only part of a task (for example "D3, Postgres part only") does not stop the rest.
4. In `status.md`, mark the task started as its "How to update this file" section says, before changing anything else. If other agents are working at the same time, follow its "Several agents at once" rule.

## 13.2 What to read

- Always: this guide, `tasks.md`, `status.md`, your phase file (its Summary and your task; other tasks in it only if your task points to them).
- Then only the files in your task's **Read** list, and only the sections it names (for example "`reference/04-file-structure.md` §4.2 only").
- Section references like §5.9, §6.U, D12 or `T2-LOOP-07` resolve through the section map in [README.md](README.md). Open the file for a reference only if your task cannot be done without it.
- Read the real code you are about to change or test. The plan describes intent; the code is the truth. If they disagree, follow 13.6.

## 13.3 Working on a task

1. Follow the task's **Steps** in order. They are written so each step leaves the repo in a working state.
2. Keep to the task's **Creates / changes** list. If you need to touch a file outside it, say so in your report; if it is in `src/monkeybot/`, follow 13.5 first.
3. Once task `P0B-06` is done, every new case you add must:
   - carry its case ID (YAML `case:` key, or `@pytest.mark.case("...")` on Python tests);
   - have its row's **Test** cell filled in `docs/testing.md` §8, in the same change.
   - For a range row (`T4-CODE-04..10`, `T4-VER-01..05`, …), give each file one expanded ID from the range (`T4-CODE-04`, `T4-CODE-05`, …) in the order the row or suite lists them, and list every file in that row's Test cell. If the files outnumber the IDs, stop and ask.
4. Run every command in the task's **Verify** list. Paste the summary lines (counts, pass/fail) into your report, not the full output.
5. Mark the task `done` in `status.md` only when every **Done when** bullet is true. Otherwise leave it `in-progress` and write what is left in a handoff entry (13.8).

## 13.4 Test failures while you build

The repo-wide rule ([reference/05.9-failure-ledger.md](reference/05.9-failure-ledger.md) §5.9.4 and [reference/10-policies.md](reference/10-policies.md) §10.6) is: failures are recorded, not fixed. Building this system is the one place where you will see many failures that *are* your job. The boundary is:

| Failure is in… | What you do |
|---|---|
| a test, scenario, fixture or helper that **your current task creates**, or the code your task is told to implement | Fix it. This is the task. |
| a test that **your task moved or rewired** (Phase 0a) and that passed before the move | Fix the move or import, never the test body or assertion. If it is not a move problem, stop and ask. |
| anything else: existing tests, other areas, other tasks' cases, live runs, flaky timing | Do not fix it. It is already in `test-runs/`. List it in your report with case id, path and the one-line error, and continue if your task does not depend on it. |

Never, under any row above:

- weaken an assertion so it passes;
- add `skip`, `xfail` or `quarantine`, or delete a test;
- regenerate a golden or baseline to match broken output;
- use `--no-verify` or `--no-ledger` to get past a gate.

If the behavior a new case expects looks wrong (the catalog Expect text, or section 6's "Asserts" column), stop and ask. Do not change the expectation yourself.

## 13.5 Guardrails

- **`src/monkeybot/` behavior does not change.** The only planned source changes are the seams listed in [reference/05.1-doubles.md](reference/05.1-doubles.md): the `now_ms` clock parameter, the multi-turn strict `ScriptedProvider`, and `MODEL_PROVIDER=fake` reading `MONKEYBOT_FAKE_PROVIDER_SCRIPT`. Each must default to today's behavior. Any other source change (for example a real bug a new case exposes) is out of scope: record the failing case, report it, and stop.
- **Goldens and baselines** change only when the task says so: goldens through `--update-golden` (refused in CI), baselines through `uv run python -m regression.t5 --suite <suite> --update-baseline` (P5-02 only). Show the diff in your report.
- **Secrets:** never print, commit, or copy `OLLAMA_API_KEY` or any other key. Live runs read it from the environment or `regression/t4/agent/.env` (gitignored) only.
- **Cross-repo:** do not edit monkeyapp unless the task says so and the user has confirmed (task `P2-10`).
- **Git:** do not commit, push, open PRs, or switch branches unless the user asked. If asked: one task = one branch `regress/<task-id>-<slug>` = one PR, unless the task says otherwise. Moves use `git mv`.
- **Live runs cost money and time.** Do not start a T4 suite or T5 trials unless the task's steps say to and the user confirmed for this session.
- **Markdown:** do not create new markdown files except the ones a task lists (`docs/testing.md`, `.cursor/rules/test-failures.mdc`). Editing existing docs that a task lists (`docs/live-evals.md`, `README.md`, `CONTRIBUTING.md`) is fine. Notes go in `status.md` handoff entries. `tasks.md` and the phase and reference files change only when the user asks for a plan change.

## 13.6 Stop and ask when

- a dependency is not `done`, or a blocking decision is still open;
- the code contradicts the plan in a way that changes what a case asserts, or a file the plan names does not exist;
- a planned case cannot be expressed with the current scenario schema, assertions, or `steps:`, and extending them is not part of your task;
- a fix would need a change in `src/monkeybot/` beyond the seams in 13.5;
- the task would delete or rewrite more than it lists, or the Phase 0a test counts do not match;
- a live run, a re-record, a golden update, or anything touching monkeyapp is the next step.

When you stop, write the question in a `status.md` handoff entry and in your reply, with the options you see. Set the task to `blocked` if it cannot continue until the user answers.

## 13.7 Standard verify commands

Use the ones your task lists; these are the full forms.

```bash
uv run ruff format --check .
uv run ruff check testkit tests regression
uv run mypy src/monkeybot testkit
uv run pytest tests -q -n auto                                   # tier 1
uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto    # all deterministic tiers (after P0B-07)
uv run pytest tests/contracts/test_catalog_pairing.py -q          # after P0B-06
uv run python -m testkit.failures latest                         # what failed in the last run (after P0A-01)
bash scripts/ci-local.sh                                         # the pre-push gate
```

Live, only when the task says so and the user confirmed:

```bash
uv run python -m regression.t4 --suite smoke --agent-url http://127.0.0.1:8787
uv run python -m regression.t5 --suite <suite> --trials <N> --agent-url http://127.0.0.1:8787
```

## 13.8 Reporting and handoff

End every task with this report in your reply:

```text
Task:      P1-04 LOOP cases (T2-LOOP-02..40)
Status:    done | in-progress | blocked (<reason>)
Changed:   <files created / moved / edited, grouped>
Verify:    <each command → summary line>
Catalog:   <rows filled in docs/testing.md, or "n/a before P0B-06">
Recorded failures not fixed: <run file + case id, path, one-line error each, or "none">
Open questions: <or "none">
Next:      <next task id per tasks.md and status.md>
```

Append the same report as a handoff entry at the bottom of `status.md` (its shape is shown there), and update the task, phase and Current rows, so the next agent can start from `status.md` alone.
