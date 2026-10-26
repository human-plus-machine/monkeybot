# Testing

This repository uses five test tiers. A case is written from the behavior in the catalog below. The folder a test lives in is its tier.

## 1. Test tiers

| Tier | Command or location | Drives | Fakes | Target |
| --- | --- | --- | --- | --- |
| T1 contract/unit | `uv run pytest tests` (`tests/unit/**`, `tests/contracts/**`) | one module, or a table across modules | `testkit` doubles | logic, contracts, schemas, goldens |
| T2 scripted trajectory | `uv run pytest regression/t2` | real `loop.run`, real in-process gateway, realtime WS | scripted provider, fake clock, fake worker | harness behavior per turn |
| T3 record/replay | `uv run pytest regression/t3` | real loop / gateway / provider adapters | recorded cassettes, golden SSE | drift against real model and SDK output |
| T4 live | `uv run pytest regression --live --suite smoke` / `python -m regression.t4` | real gateway + real provider | fixture workspaces only | model + harness on real tasks |
| T5 trials | `python -m regression.t5 --suite ... --trials N` | T4 scenarios × N | none | pass rate vs keyed baseline |

`regression/selftest/**` exercises the regression machinery itself and is T1.

## 2. Commands and gates

```bash
uv run pytest tests -n auto
uv run pytest tests regression -m "t1 or t2 or t3" -n auto
uv run pytest regression/t2 -k gateway
uv run pytest regression --live --suite smoke --agent-url http://127.0.0.1:8787
uv run python -m regression.t5 --suite coding_hard --trials 8 --agent-url http://127.0.0.1:8787
uv run pytest regression/t3 --record -k ollama_cloud
uv run python -m testkit.failures latest
bash scripts/ci-local.sh
```

`uv run pytest tests -n auto` is the fast T1 loop. `tests regression -m "t1 or t2 or t3"` is the deterministic set that runs on every push. `--live` collects T4 and T5. `--record` re-records T3 cassettes. `--update-golden` rewrites goldens and refuses to run when `CI` is set. `--trials`, `--suite`, and `--agent-url` select a live run. Markers are `t1` through `t5`, `live`, `needs_postgres`, `needs_firestore`, `darwin_only`, `slow`, `quarantine`, and `case(id)`.

`bash scripts/ci-local.sh` is the local pre-push gate. It runs the deterministic pytest set, and also format, mypy, the `cli/` suite, the browser-mcp suite, and the eval self-checks. `MONKEYBOT_TEST_TRIGGER` is the job name. Every monkeybot job below runs with the failure ledger on and keeps `test-runs/` so a failure can be triaged with `python -m testkit.failures`.

| Gate | Trigger | Runs | Budget |
| --- | --- | --- | --- |
| Pre-commit | every commit touching `src/monkeybot`, `testkit`, `tests`, or `regression` | changed test files, otherwise a collection check of `tests` and `regression` | seconds |
| Pre-push (`scripts/ci-local.sh`) | every `git push` | `pytest tests regression -m "t1 or t2 or t3"` | under 5 minutes |
| `ci.yml` | `workflow_dispatch` today | the same set as pre-push | under 10 minutes |
| `storage-parity` | PRs touching `core/persistence/**` or `core/memory/**`, and nightly | `tests/contracts/test_storage_parity.py`, plus the `needs_postgres` and `needs_firestore` marks, with Postgres and the Firestore emulator | under 10 minutes |
| `cassette-staleness` | PRs touching any `uv.lock` | `regression/t3/tools/check_staleness.py` | under 1 minute |
| `live-smoke` | develop to main, lockfile PRs, push to main, manual | `regression --live --suite smoke` | 30–45 minutes |
| `live-trials` | nightly, manual, and PRs into `main` | `python -m regression.t5 --suite <s>` once per T4 suite (`coding_hard`, `drift`, `subagents`, `memory`, `mcp`), compared with `regression/t5/baselines/` | hours |
| monkeyapp `contract` | every monkeyapp PR | vendored schema validation of the fake gateway and dispatcher | under 1 minute |

`storage-parity`, `cassette-staleness`, and `live-trials` land with the tasks that build them. Live jobs read `OLLAMA_API_KEY` from the environment. The other jobs use no secrets. The monkeyapp contract job lives in monkeyapp.

## 3. Harness architecture

`testkit/` holds the doubles. Import them from their modules. `testkit/__init__.py` stays empty so the ledger plugin can load in `cli/` and browser-mcp, which do not install this repo's third-party dependencies.

- `testkit/provider.py`: scripted and capturing providers, including the strict multi-turn `ScriptedProvider`.
- `testkit/history.py`: `FakeHistory`, one in-memory history store.
- `testkit/executors.py`: `RecordingExecutor` with scripted tool envelopes.
- `testkit/inspectors.py`: allow, deny, confirm, and order-recording inspectors.
- `testkit/context.py`: `make_ctx`, `make_loop_ctx`, and `ctx_with_task`.
- `testkit/mcp.py`: `NoMCP`, `RecordingMCP`, and `FakeMCPClient`.
- `testkit/memory.py`: memory-subsystem helpers.
- `testkit/palace.py`: `InMemoryPalace`.
- `testkit/hooks.py`: hook payload builder and `RecordingHook`.
- `testkit/verifier.py`: scripted classifier, scripted judge, and mailbox helpers.
- `testkit/sandbox.py`: a stand-in OpenSandbox module.
- `testkit/conversations.py`: fixed multi-turn message fixtures.
- `testkit/gateway_ports.py`: `FakeLoopPort` for in-process gateway tests.
- `testkit/scrub.py`: secret scrubber used by the ledger.
- `testkit/failure_ledger.py`: the pytest plugin and `LedgerWriter`.
- `testkit/failures/`: `python -m testkit.failures`, and `catalog.py`, which reads §8.

`regression/schema/` is the scenario model (`scripted`, `replay`, or `live`), the YAML event vocabulary, mid-run steps, assertions, and the invariants that run on every scripted scenario. `regression/plugin.py` collects scenario YAML as pytest items and registers `--live`, `--record`, `--update-golden`, `--trials`, `--suite`, and `--agent-url`.

- `regression/drivers/loop.py`: drives `loop.run` for one scripted scenario and then runs invariants and assertions.
- `regression/drivers/record.py`: `EvalRecord` and `EvalHook`. They collect loop-visible signals and leave harness behavior unchanged.

Install a fake clock before importing the scheduler engine, so the engine does not bind the real clock at import. Settle hooks before asserting on the turn.

## 4. Authoring rules

The folder is the tier. `tests/**` is T1. `regression/t2/**` is T2, and the same pattern holds for `t3`, `t4`, and `t5`. `regression/selftest/**` is the exception: it tests this machinery and is marked T1.

One case, one scenario file. The file name is the slug (`doom_identical_read.yaml`). The case ID (`T2-LOOP-07`) lives in the YAML `case` field, or in `@pytest.mark.case(...)` on a Python test, and in the §8 row. IDs are never reused. A range row such as `T4-CODE-04..10` stays one row; each file takes one expanded ID from the range, in the order the row lists them, and every file is named in that row's Test cell.

Put a bug-fix case at the lowest tier that can express it: T1, then T2, then T3. Use T4 only when the bug is about model behavior. Set `regression_of` to the commit or PR.

Assert the resulting state first. Add a mechanism assertion only where a caller would notice that mechanism failing. Keep the run deterministic: a fake clock, port 0, and fixed ids.

A new test that fails is evidence to diagnose, not a reason to weaken the expected outcome. Fix incorrect wiring. Track incorrect product behavior on its own. Ask when the intended behavior is ambiguous.

When intended behavior changes, update the catalog Expect cell first, then the test, in the same change. Removing a flow removes the catalog row and the test together.

`--update-golden` rewrites goldens locally and refuses to run when `CI` is set. Golden diffs are reviewed like code. Re-record a cassette only with `MONKEYBOT_RECORD_CASSETTES=1`, followed by a scrub pass. A re-record change contains the cassette and the reason. A strict replay failure (`cassette_stale`) is fixed by re-recording.

T1–T3 have zero flake tolerance. A flaky deterministic test is a bug, usually a missing fake clock or a missing settlement wait. A temporary `quarantine` marker carries an issue link and an expiry date. T4 flakiness is handled by T5 trials, not by retries.

A failure is recorded and reported. Fixing it is a separate task that starts from `python -m testkit.failures prompt <fingerprint>` and ends with `mark <fingerprint> --status fixed`. The fix changes the code under test. An assertion changes only when the Expect cell is wrong, and that cell changes first, in the same change. `--no-verify`, `--no-ledger`, a new `skip`, `xfail`, or `quarantine`, and deleting a test are not ways to get past a failure.

## 5. Failures and the ledger

A failing test, scenario, or trial is a finding to report. The run writes a file under `test-runs/`. The gate still fails: pytest exits non-zero, `scripts/ci-local.sh` fails, and the pre-push hook blocks the push.

When at least one failure was recorded, the plugin prints:

```text
==================== monkeybot failure ledger ====================
4 failures recorded to test-runs/20261025T153012Z/20261025T153012Z-3f2a9c1-48211.jsonl
  T2-LOOP-07  regression/t2/loop/scenarios/loop/doom_identical_read.yaml   AssertionError
  T1-GW-01    tests/contracts/test_sse_schema.py::test_every_kind           ValidationError
  ...
These failures are recorded for later triage. Agents: do not change code or
tests to fix them in this task. Report the run file and continue (docs/testing.md §5).
Inspect: uv run python -m testkit.failures latest
=================================================================
```

The always-on rule `.cursor/rules/test-failures.mdc` says the same thing in short form:

- Report the run file and each failure's case id, path, and one-line error. Then continue the original task, or stop when the task cannot continue without the failing behavior.
- Leave source, tests, scenarios, goldens, cassettes, and baselines as they are.
- Leave `skip`, `quarantine`, `xfail`, and the assertion strength as they are.
- The exception is an explicit request to fix failures, for example "fix the failures in run X" or "fix fingerprint abc123". The brief for that work is `python -m testkit.failures prompt <fingerprint>`.
- Running the tests again to confirm a failure is fine. Changing the tree between those runs is a different task.

`.cursor/hooks/test-failure-notice.py` runs on `postToolUse` and `postToolUseFailure` for `Shell`. It watches test commands (`pytest`, `regression.t4`, `regression.t5`, `ci-local.sh`, `git push`). When `test-runs/LATEST.json` shows a finished run with failures that this hook has not yet reported, it returns `additional_context` with the same instruction and the run path. The hook fails open. It stores the last run it reported under `.cursor/hooks/.state/`, which is gitignored. The `pre-pr.py` hooks stay as they are.

| Command | Does |
| --- | --- |
| `latest` | Failures of the newest run, or `--group` for a whole gate run, grouped by tier then area. |
| `open` | Distinct failures, by fingerprint, that have not passed since. A fingerprint stays open until a later run's `outcomes.tsv.gz` shows that nodeid passing. Shows first seen, last seen, occurrence count, and triage status. |
| `show <fingerprint or run:seq>` | The full record, including the traceback and the repro command. |
| `prompt <fingerprint>` | A fix brief: case id, tier, the catalog Expect text, the error, the traceback, the repro command, related files, and the rule to fix the code under test unless the catalog Expect is wrong. |
| `mark <fingerprint> --status investigating\|fixed\|wontfix\|quarantined --note "..." [--link URL]` | Writes `triage.json`. `quarantined` matches a `quarantine` marker that has an issue link and an expiry. |
| `export --format md\|json` | Prints a triage report to stdout. It does not write a file. |
| `prune` | Applies retention now: the newest 200 runs, or 30 days, whichever keeps more. |

## 6. Catalog pairing

The catalog is §8. `tests/contracts/test_catalog_pairing.py` is T1 and runs on every push. It reads §8 through `testkit/failures/catalog.py` and every case ID a test cites. YAML scenarios cite an ID with the `case:` key. Python tests cite one with `@pytest.mark.case(...)`.

A case ID inside a range row covers that row. `T4-CODE-05` covers `T4-CODE-04..10`. One row may name several files, separated by commas.

The test fails when:

- a test cites an ID that is not in the catalog;
- an ID is cited by a file that is not listed in that row's Test cell;
- a Test cell names a path that does not exist;
- an ID appears twice in the catalog, or an ID's tier prefix disagrees with the folder it lives in.

The tier prefix is the folder. `tests/**` is T1. `regression/t2/**` is T2, and the same for `t3`, `t4`, and `t5`. `regression/selftest/**` is T1.

Rows with an empty Test cell are uncovered. The test prints the uncovered count per area and tier and still passes. That count is the progress meter while the later phases fill the Test column.

Keep the `## 8.` and `## 9.` headings, and the pipe-delimited columns `ID`, `Case`, `Expect`, `Tier`, `Pri`, `Origin`, and `Test`, stable. The parser uses those boundaries.

## 7. Backfill rationale

A bug-fix change adds a case that fails before the fix and passes after, at the lowest tier that can express it. The scenario sets `regression_of` to the commit or pull request. This section lists that value and the case that now guards it. When a regression can no longer be expressed at the current HEAD, the row says why.

No scenario in the tree sets `regression_of` yet.

| regression_of | Case or reason |
| --- | --- |

## 8. Case catalog

Priority: P0 means a regression here means the harness is broken or unsafe. P1 means a feature is broken. P2 means polish or a rarely used path. The Expect column is the behavior spec. An empty Test cell is uncovered; the pairing test counts it and still passes.

### Turn loop (LOOP)

Existing unit coverage: `tests/unit/core/messages`, `tests/unit/core/runtime`, `tests/unit/core/tools`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-LOOP-01 | Turn completion | Text-only turn completes, one provider call. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. | regression/t2/loop/scenarios/loop/turn_completion.yaml |
| T2-LOOP-02 | Max turns cap | Tool call every turn with `max_turns: 2`: interventions `[max_turns]`, `Error` text exactly `Max turns exceeded`. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-03 | Max turns not on clean finish | Text finish on the last allowed turn emits no `max_turns`. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-04 | Truncated batch rejected | `done: {truncated: true}` + tool call: `truncated_batch` detail `stream_truncated`; executor never called; error `ToolCallResult` emitted. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-05 | All parse error batch rejected | Every call has `parse_error`: `truncated_batch` detail `all_parse_error`. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-06 | Mixed parse error batch runs valid | One valid + one broken call: valid one executes. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-07 | Doom identical read | Three identical `read_file` calls: `doom_loop`; next provider request has empty tools; recovery note in system text. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-08 | Doom path retry spelling | Three failing `replace_in_file` on `./a` vs `a` with different hunks: `doom_loop`. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-09 | Doom path reset on success | Fail, fail, success, fail on one path: no trip. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-10 | Doom exempt poll | `loop_status` / `await_command` between failing edits neither hides nor trips the streak. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-11 | Doom disabled | `DOOM_LOOP_THRESHOLD=0`: no trip after 5 identical calls. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-12 | Empty then text | Silent `done`, then text: `empty_completion` detail `retry`; no `Error`. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-13 | Empty exhausted | Three silent `done`s: `empty_completion` detail `exhausted`; exact error string. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-14 | Post tool empty retry | Tool call, silent `done`, text: `post_tool_empty`; history still holds the tool row. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-15 | Thinking only is empty | Thinking deltas then `done` with no text: treated as empty, retried. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-16 | Background nudge then release | Executor reports a job for two checks: `background_jobs_nudge` ×2; held text absent from events; later text released. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-17 | Background check raises fails closed | `running_background_jobs()` raises: `Error` `jobs_check_failed`; no clean finish. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-18 | Background nudges exhausted | Job never ends: 3 nudges then kill error; no double reporting. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-19 | Steer mid turn | `steps: steer` after first tool batch: `UserSteered` before provider call 2; steer text in that request's last message. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-20 | Promote follow up provenance | Promoted follow-up: `UserSteered.queued_request_id` set; not duplicated. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-21 | Cancel mid stream | `steps: cancel` after first text delta: partial assistant row persisted; `Error` `Request cancelled`; no tool executes. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-22 | Cancel after tool call before execute | Synthetic cancel `ToolResponse` in history; invariant 3 holds. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-23 | Confirm approve | Confirm inspector + approve: tool executes once. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-24 | Confirm deny | Deny: no execute; error tool row; turn continues. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-25 | Confirm timeout | No answer: synthesized error; not executed. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-26 | Confirm always remembered | `always: true`: second identical call in the same session skips the confirm. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-27 | Parallel results in submission order | Two parallel-safe calls then a mutating call: results in one user row in submission order. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-28 | Parallel concurrency cap | 12 parallel-safe calls with `MONKEYBOT_PARALLEL_TOOL_CONCURRENCY=10`: never more than 10 in flight. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-29 | Registry refresh same turn | `enable_mcp` success: next provider request includes the new tools. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-30 | Repair orphan tool response | Preloaded orphan `ToolResponse`: provider sees synthetic request; stored rows unchanged. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-31 | Repair missing tool result | Preloaded `ToolRequest` without result: provider sees interrupted-result text. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-32 | Ask user roundtrip | Scripted `ask_user`: `AskUserRequest` event; preset answer becomes the tool result; turn completes. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-33 | Ask user non interactive | No pending bus: tool error, no hang. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-34 | Thinking block order | Thinking deltas then text then tool: block start/delta/complete ordering; thinking closed before tools. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-35 | Thought signature preserved | `ToolCall.metadata.thought_signature` on first call survives into the next request. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-36 | Usage totals | Two calls with `usage` events: `TurnComplete.usage` sums; missing usage stays 0. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-37 | Provider raises | `raise` mid-stream: `Error` then `TurnComplete`; partial text persisted. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-38 | Tool input delta passthrough | `tool_input_delta` events are forwarded and do not execute anything before `ToolCall`. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-39 | Grounding event passthrough | `grounding` event yields `GroundingEvent` with sources unchanged. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T2-LOOP-40 | Resume thinking budget routine | `MONKEYBOT_RUN_KIND=routine` resume turn on gemini/claude/ollama sets the resume thinking budget; other providers untouched. | T2 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T3-LOOP-01 | Replay live doom loop | A recorded live run that tripped the doom loop still trips it at the same call index. | T3 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |
| T3-LOOP-02 | Replay live truncation | A recorded `finish=length` tool batch is still rejected. | T3 | P0 | `core/runtime/loop.py`, `turn_loop.py`, `tool_dispatch.py`, `tool_batch.py`, `doom_loop.py`, `loop_usage.py`, `input_admission.py`, `provider_stream_mapper.py`, `tool_integrity.py`. |  |

### Context (CTX)

Existing unit coverage: `tests/unit/core/context`, `tests/unit/core/prompts`, `tests/unit/core/runtime`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-CTX-01 | Harness prompt golden | `compose_stable_baseline` for 6 fixed `TurnContext`s (memory on/off, sandbox on/off, task tool on/off, MCP catalog, emission styles) equals committed goldens. Any prompt edit shows up as a reviewed golden diff. | T1 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T1-CTX-02 | Volatile tail parts golden | `compose_volatile_tail_parts` output for fixed date/memory/skills/todos. | T1 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-01 | Compact at trigger | `config: model.context_window: 4000` + long tool results: `ContextSummarizing` → `ContextSummarized`; one summary row persisted. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-02 | Compact pins verdict attachment thinking | Verdict row, attachment descriptor, tail thinking survive compaction. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-03 | Compact does not persist repairs | Repaired tool pairs are not written back. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-04 | Compact keep tail ratio | Tail keeps `max(2, 20%)` rows and never cuts a tool pair. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-05 | Compact fallback | Summarizer `raise` + > 5000 preloaded rows: `compaction_fallback`; pins kept; `ContextEpochStarted`. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-06 | Compact trailer from goal ledger | Summary row carries the workspace trailer. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-07 | Summarizer model override | `model.summarization_model` is the model of the summary call; summary call has no `vertex_google_search`. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-08 | Background summary applied | At the moderate bar a background summary is applied on the next turn when the prefix matches. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-09 | Background summary stale fallback | After a rewind, the stale background summary is discarded and inline compaction runs. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-10 | Epoch volatile update | Memory index changes between inner turns: `SystemContextUpdated` with `changed_sources: [memory]`; stable prefix bytes identical. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-11 | Epoch stable change | `AGENT.md` / `AGENTS.md` change between turns: `ContextEpochStarted`. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-12 | Epoch after compaction | Compaction opens a new epoch. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-13 | Stable prefix byte stable | Across 4 inner turns with tool calls, request 1..4 share an identical stable system prefix. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-14 | Project instructions spoof heading | `AGENTS.md` containing `## Runtime notes` does not land in the volatile tail. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-15 | Slash skill turn scoped | `/diagnostics` adds an invoked-skill section on that turn only. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-16 | Tool result shaping under pressure | Moderate pressure: tool result trimmed head 70 / tail 30; spill-inventory note kept. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-17 | Base64 sanitized | Tool result with a base64 blob is redacted before the provider (`MONKEYBOT_TOOL_RESULT_SANITIZE` default). | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-18 | Context usage current not peak | `ContextUsage.estimated_tokens` drops after compaction. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T2-CTX-19 | Memory wake up large | Large wake-up index injected; turn completes. | T2 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. | regression/t2/loop/scenarios/ctx/memory_wake_up_large.yaml |
| T3-CTX-01 | Replay live compaction | Recorded long-session cassette compacts at the same call index. | T3 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T4-CTX-01 | Summarization trigger | `config: model.context_window` small enough that the scenario must compact; `min_summarizations: 1`. Fails if it would have passed without compacting. | T4 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T4-CTX-02 | Post compaction continuity | Multi-step task spanning a forced compaction still completes (workspace grader). | T4 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |
| T5-CTX-01 | Trials of T4-CTX-02 | Capability. | T5 | P1 | `core/context/*` (`context_budget.py`, `history_compaction.py`, `background_summary.py`, `epoch.py`, `prompt.py`, `project_context.py`, `slash_skills.py`), `core/prompts/*`, `tool_output_policy.py`, `tool_shapers.py`, `tool_result_ingress.py`. |  |

### Hooks (HOOK)

Existing unit coverage: `tests/unit/core/context`, `tests/unit/core/hooks`, `tests/unit/core/runtime`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-HOOK-01 | Hook lifecycle order | Recording hook sees `USER_MESSAGE, PRE_TURN, TOOL_DEFINITION, BEFORE_PROVIDER_REQUEST, AFTER_PROVIDER_RESPONSE, PRE_TOOL, POST_TOOL, …, PRE_FINAL_ANSWER, POST_TURN` in that order for a one-tool turn. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-02 | Pre turn once | `PRE_TURN` fires on inner turn 1 only. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-03 | Hook exception is logged | Raising hook does not abort the turn. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-04 | Hook timeout continues | Hook exceeding its timeout is abandoned; turn continues. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-05 | Settlement before turn complete | Slow `POST_TOOL` write-side hook completes before `TurnComplete`. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-06 | Tool definition replacement | Hook that replaces tools changes the next request's tool list. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-07 | History compacted awaited | `HISTORY_COMPACTED` hook runs before the post-compaction provider call. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-08 | Evidence missing path | `hooks: [evidence_guard]`, answer with `Evidence: missing.py`: correction in the next request's system text; only once. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-09 | Evidence unread then read | Citation of an unread real file: notice once; after `read_file`, no notice. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-10 | Evidence grep only not accepted | File only seen via `grep` still counts as unverified. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-11 | Evidence thread isolation | Reads in session A do not satisfy citations in session B. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-12 | Secret redacted interactive | Tool result containing a canary: `CredentialEgressBlocked`; next request redacted; turn continues. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-13 | Secret ends routine turn | `MONKEYBOT_RUN_KIND=routine`: `Error` and no second provider call. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-14 | Secret scanner failure fails closed | Scanner raises: content withheld. | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T2-HOOK-15 | Secret in tool args | Secret in `write_file.content`: `credential_egress_blocked`; file not created (also POL-09). | T2 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |
| T4-HOOK-01 | Evidence citation live | Ask for an answer with `Evidence:` paths; every cited path exists and was read (code grader). | T4 | P0 | `core/hooks/__init__.py`, `core/hooks/evidence_guard.py`, `core/runtime/loop_hooks.py`, `secret_egress.py`, settlement. |  |

### File tools (FILE)

Existing unit coverage: `tests/unit/core`, `tests/unit/core/tools`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-FILE-01 | Tool error code catalog | Every `details.code` raised in `src/` (AST scan of `WorkspaceError(...)`, `PatchError(...)`, ledger errors) is in Appendix A's enum; an undocumented new code fails. | T1 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T1-FILE-02 | Envelope shape contract | Every built-in tool error has `ok:false`, `error_kind ∈ {validation, policy, runtime, incomplete_scan, credential_egress_blocked}`, `message`, `hint`. Lists the known exceptions (`enable_mcp`, some `load_file`, `computer_*`) explicitly so a new exception fails. | T1 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-01 | Tool read | Content of a fixture file appears in the result. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. | regression/t2/loop/scenarios/file/tool_read.yaml |
| T2-FILE-02 | Read paging no gap | Two pages via `next_offset` concatenate to the file. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-03 | Read offset past eof | `ok` with empty content (pins current behavior). | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-04 | Read escape dotdot | `../outside`: `invalid_path`; no outside bytes in any result. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-05 | Read escape symlink | `link_out/x`: `path_escape`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-06 | Read absolute needs grant | Absolute path outside workspace: `path_needs_grant`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-07 | Read huge single line | Sliced with marker; no `next_offset`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-08 | Tool write | File created with exact bytes. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. | regression/t2/loop/scenarios/file/tool_write.yaml |
| T2-FILE-09 | Write new without read ok | New path writes without a prior read. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-10 | Overwrite unread rejected | `read_before_write`; bytes unchanged. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-11 | Stale read rejected | `steps: touch_file` between read and replace: `stale_read`; bytes unchanged; re-read then replace succeeds. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-12 | Skills read only | Write to `skills/x/SKILL.md`: `skills_read_only`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-13 | Write outside scope | `WORKSPACE_WRITE_SCOPE_REL=src`, write `wide.txt`: `write_outside_scope`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-14 | Write payload too large | Over `WORKSPACE_WRITE_MAX_BYTES`: `payload_too_large`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-15 | Replace error codes | One scenario per code: `not_found_replace`, `ambiguous_replace`, `no_change`, `empty_old_string`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-16 | Replace fuzzy unicode | Smart quotes in `old_string` match ASCII in file; `match_mode` reported. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-17 | Replace apply patch | Both edits land. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. | regression/t2/loop/scenarios/file/tool_replace_apply_patch.yaml |
| T2-FILE-18 | Patch fail closed | One bad hunk: `hunk_not_found`; no file changed. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-19 | Patch rollback | Injected mid-apply failure: `rollback_failed`; `details` lists dirty paths. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-20 | Patch add existing / patch move dest exists | `already_exists`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-21 | Patch update requires read | Update/Delete of unread file: `read_before_write`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-22 | Glob skips noise | `**/*` omits `node_modules`, `.venv`, `build`; `node_modules/**` includes it. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-23 | Glob cap incomplete | Over `WORKSPACE_GLOB_MAX_PATHS`: `ok:false`, `incomplete_scan`, never an empty success. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-24 | Grep gitignore default | Default misses `secret.txt`; `respect_gitignore:false` finds it; nested `pkg/.gitignore` scoped. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-25 | Grep incomplete scan | Oversized file in scope: `incomplete_scan`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-26 | Grep pagination | `max_matches` + `offset` page through all matches; `total_match_count` stable. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-27 | Grep rg vs python parity | Same results with rg present and absent for an rg-safe pattern; lookaround pattern uses Python. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-28 | Grep invalid regex | `invalid_regex`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-29 | List dir truncate | 301 entries: `truncated:true`, `count:301`. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-30 | Virtual mounts | `skills/` and `artifacts/` resolve to their roots; no fallback between roots. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-31 | Spill large tool output | Over `spill_threshold`: spill path returned; follow-up `read_file` of the spill path works past the default line cap. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-32 | Spill session id sanitized | Session id with `..` cannot write outside the spill tree. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T2-FILE-33 | Tool glob grep | As today: glob and grep both return the fixture's matches. | T2 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. | regression/t2/loop/scenarios/file/tool_glob_grep.yaml |
| T4-FILE-01 | Core read | Plus: answer quotes a fixture line (code grader). | T4 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T4-FILE-02 | Core read error | Model recovers from a bad path; `max_tool_errors: 0` after forgiveness. | T4 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T4-FILE-03 | Core write | Plus: `workspace_file_contains`. | T4 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |
| T4-FILE-04 | Grep absence honesty | After `incomplete_scan`, the answer does not claim absence (`response_not_contains` + judge). | T4 | P0 | `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`. |  |

### Shell (SHELL)

Existing unit coverage: `tests/unit/core/tools`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-SHELL-01 | Run command | `echo` output and `exit_code: 0`. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. | regression/t2/loop/scenarios/shell/run_command.yaml |
| T2-SHELL-02 | Argv forms | `argv` list, JSON string, `command`+`args`, `shell` all parse and run. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-03 | Allowlist deny | `curl`: `error_kind: policy`; not executed (no side-effect file). | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-04 | Shell wrapper bypass | `bash -c 'pip install x'`: denied by deny patterns before execute. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-05 | Unscreenable script fails closed | Deeply nested `bash -c` beyond the walk depth: denied. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-06 | Home escape | `cat ~/.ssh/id_rsa` and `bash -c 'cat $HOME/.ssh/x'`: no bytes read (path policy or jail). | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-07 | Cwd escape | `cwd: ../`: `invalid_cwd`. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-08 | Nonzero exit | `python -c 'raise SystemExit(3)'`: `ok:false`, `exit_code:3`, streams present. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-09 | Timeout partial output | Timeout: error with `partial_output_path`; process group gone. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-10 | Large stdout spill keeps exit | Huge stdout: `output_path` set; real `exit_code` preserved. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-11 | Bg start await cursor | `background:true` then two `await_command`s: second page `has_more:false`, no duplicated prefix. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-12 | Bg job cap | Job over `max_background_jobs`: cap error. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-13 | Bg kill command | `kill_command` stops the job; `await_command` reports killed. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-14 | Bg killed at turn end | Job still running at end: killed by `aclose`; turn guard nudged first (LOOP-16). | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-15 | Mempalace search only | `mempalace edit\|update\|forget\|repair`: `SecurityError`; `search` allowed when memory on. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-16 | Mempalace refused memory off | Memory off: policy error, no spawn. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T2-SHELL-17 | Jail fail open pinned | When the jail cannot start, caches are not rewritten and a warning is logged. Skip on hosts without seatbelt/namespaces. | T2 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T3-SHELL-01 | Replay live pytest run | Recorded live `coding/fix_failing_test` cassette replays against `calc_repo` and ends with pytest green. | T3 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T4-SHELL-01 | Core run command | Plus: a file created by the command exists. | T4 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |
| T4-SHELL-02 | Core run command sandbox | Same through OpenSandbox. | T4 | P0 | `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`. |  |

### Policy (POL)

Existing unit coverage: `tests/unit/core/tools`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-POL-01 | Policy decision table | Table-driven: (tool, args, policy fixture) → `allow\|deny\|confirm` + `grant_kind` for ~40 rows across all inspectors. One place to see the whole policy. | T1 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T1-POL-02 | Empty allowed commands raises | Loading an allowlist with `allowed_commands: []` raises. | T1 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T1-POL-03 | Corrupt grants load empty | Bad JSON in `grants.json` loads as empty, logs once. | T1 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-01 | Unknown binary confirm approve | Unlisted binary: `ToolConfirmationRequest`; approve executes. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-02 | Unknown binary confirm deny | Deny: not executed. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-03 | Default deny patterns | Omitted `deny_patterns`: `pip install`, `uv add`, `npm i`, `brew install` denied. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-04 | Path grant read | Absolute read without grant: `path_needs_grant`; with `turn_path_grants`: reads in place. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-05 | Granted folder credential denied | `.env` in a granted folder: `credential_denied`; omitted from `grep` results. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-06 | Symlink in grant does not widen | Symlink inside grant pointing outside: denied. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-07 | Command grant from store | Binary listed in `grants.json` runs without confirm. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-08 | Grant revocation clears | Removing a path grant between turns removes read access (replace, not merge). | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-09 | Permissions last match wins | Two overlapping rules: last wins. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-10 | Permissions missing file fail open | Pins current behavior (documented risk). | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |
| T2-POL-11 | Start loop always confirms | `start_loop` always yields a confirmation, even with an "always" approval. | T2 | P0 | `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`. |  |

### Coding (CODE)

Existing unit coverage: `tests/unit/core/coding`, `tests/unit/core/context`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-CODE-01 | Read before write | `read_before_write` code; pytest result as today. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. | regression/t2/loop/scenarios/coding/read_before_write.yaml |
| T2-CODE-02 | Gate rejects unverified | `interventions: [final_answer_gate]`, `provider_calls: 5`. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. | regression/t2/loop/scenarios/coding/gate_rejects_unverified.yaml |
| T2-CODE-03 | Gate passes when verified | No intervention. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. | regression/t2/loop/scenarios/coding/gate_passes_when_verified.yaml |
| T2-CODE-04 | Fix failing test | `workspace_command_passes`. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. | regression/t2/loop/scenarios/coding/fix_failing_test.yaml |
| T2-CODE-05 | Gate masked exit | `pytest && true` and `pytest \| tee x` do not count as verification. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-06 | Gate non executing wrapper | `cat pytest.log`, `echo pytest`, `git log` do not count. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-07 | Gate background verify not counted | `pytest` with `background:true` does not clear the gate. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-08 | Gate write after verify | Verify, then another write: gate holds again. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-09 | Gate open todos reject | Todo `in_progress` touched this message: rejected; untouched todos do not gate. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-10 | Gate question not gated | Pure question with no writes: no gate. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-11 | Gate cap then accept | `max_rejections: 1`: second final answer accepted and streamed. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-12 | Gate custom verify commands | `coding.verification_gate.verify_commands: ["just check"]` counts. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-13 | Gate rejected answer not persisted | Rejected text is not in history. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-14 | Git context note | Dirty fixture repo: `git: <branch>` line in `## Runtime notes`. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-15 | Git context absent | Non-repo / `git` missing / timeout: no note, no error. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-16 | Git env isolation | `GIT_DIR` in the environment does not leak into the status call. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-17 | Compaction forgets reads | After compaction, editing a previously read file returns `read_before_write`. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T2-CODE-18 | Subagent has ledger no gate | Child edits are ledger-gated; child final answer is not gate-held. | T2 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T4-CODE-01..03 | Coding/* | As today. | T4 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T4-CODE-04..10 | Coding hard/* | As today, oracle self-test kept. | T4 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |
| T5-CODE-01 | Trials coding + coding hard | Capability, N = 8; first baseline per (provider, model). | T5 | P1 | `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`. |  |

### Attachments (ATT)

Existing unit coverage: `tests/unit/core/attachments`, `tests/unit/gateway/sse`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-ATT-01 | Load image preview | PNG via `path`: next request contains an image block of preview size; original bytes stay in the store. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. | regression/t2/loop/scenarios/attachments/load_image_preview.yaml |
| T2-ATT-02 | Load pdf | PDF via `attachment_id`: file block in request. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-03 | Load text rejected | `.py` path: error telling the model to use `read_file`. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-04 | Load both args | `path` + `attachment_id`: error. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-05 | Load per reply cap | 6th image in one reply: cap error. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-06 | Freeze descriptors in history | After the turn, history holds descriptors, not media bytes. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-07 | Attachment symlink leaf rejected | Symlinked store leaf: rejected. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-G1 | Upload then reply ref | `POST /attachments` then `/reply` with `attachmentRef`: `AttachmentDescriptor` at turn end. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T2-ATT-G2 | Upload limits | 413 over size; 415 bad type; 404 when `ATTACHMENTS_ENABLED=false`; `INLINE_ATTACHMENT_NOT_ALLOWED`, `AMBIGUOUS_REPLY_BODY`, `ATTACHMENT_NOT_FOUND`. | T2 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |
| T4-ATT-01 | Describe image live | Model names the dominant color of a fixture image. | T4 | P1 | `core/attachments/*`, `_tool_load_file`, `freeze_attachments_in_history`, gateway upload route. |  |

### Computer use (COMP)

Existing unit coverage: `tests/unit/computer`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-COMP-01 | Not advertised off darwin | `should_enable_computer_tools` false on non-darwin regardless of config. | T1 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |
| T2-COMP-01 | Open exec suffix denied | `computer_open x.sh`: `error_kind: policy`. | T2 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |
| T2-COMP-02 | Denied dirs and globs | `~/.ssh`, `~/.aws`, `*.pem`, `.env`: denied for list/find/move/trash. | T2 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |
| T2-COMP-03 | Denied apps and schemes | Terminal / Cursor apps; `file:` URL: denied. | T2 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |
| T2-COMP-04 | Clipboard cap | 20 001-char write: rejected. | T2 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |
| T2-COMP-05 | Move trash not always scoped | "Always allow" on `computer_trash` does not persist. | T2 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |
| T2-COMP-06 | Trash huge tree refused | `check_trashable` refuses. | T2 | P2 | `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`. |  |

### Subagents (SUB)

Existing unit coverage: `tests/unit/core/subagents`, `tests/unit/core/tools`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-SUB-01 | Child tool list | Worker `build_context` tool names contain no `task`, no loop tools, no `computer_*` (behavioral, not AST). | T1 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-01 | Subagent roundtrip | `SubagentStarted` → `SubagentEvent`s → `SubagentCompleted ok:true`; `progress.jsonl` and `output.json` written. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. | regression/t2/loop/scenarios/subagents/subagent_roundtrip.yaml |
| T2-SUB-02 | Nested task refused | Fake child tries `task`: unknown-tool error in child; exactly one `SubagentCompleted`. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-03 | Expect files present / expect files absent | `artifact_exists` true / false; `ok` reflects child exit, not the missing file. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-04 | Expect files escape | `expect_files: [../x]`: validation error. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-05 | Unknown subagent type | Validation error; no spawn. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-06 | Subagent timeout | Hanging child, `subagents.timeout_sec: 2`: `ok:false`, error mentions timeout; process group gone. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-07 | Parent cancel | Cancel during child run: `task: cancelled (parent)`; child killed. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-08 | Child errors record failed | Clean exit with errors in payload: recorded as failed. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-09 | Large ndjson line | 1 MiB line parses (limit is 16 MiB, not 64 KiB). | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-10 | System prompt redacted on pipe | `SystemPromptSnapshot.text` redacted in forwarded events. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-11 | Parallel task cap | 12 `task` calls in one batch: at most 10 concurrent. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-12 | Persona applied | Persona `agent_md` / `model` / `skills` reach the child envelope. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-13 | Child memory read only | Child has palace read, no ingest hook, no writer. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-14 | Queue pending not success | `MONKEYBOT_TASK_QUEUE=1`: pending result with `queued`, `run_id`, `child_thread_id`; no `SubagentStarted`; run row `pending`. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-15 | Worker pool executes | Worker pool claims the pending run, writes `progress.jsonl`, records completion. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-16 | Worker stale reclaim | Claim older than `MONKEYBOT_WORKER_STALE_CLAIM_MS` (fake clock): reclaimed once. | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T2-SUB-17 | Ledger context forwarded | Verifier ledger context reaches the `task` envelope (`format_subagent_context`). | T2 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T3-SUB-01 | Replay ndjson tape | Recorded child NDJSON tape through the parent parser: same `progress.jsonl` bytes and same final event. | T3 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T4-SUB-01 | Dispatch complete | As today. | T4 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T4-SUB-02 | Persona dispatch | `required_tools: [task]`; child transcript manifest `subagent_type` matches persona. | T4 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |
| T5-SUB-01 | Trials subagents | Reliability on `min_subagent_calls: 1`; zero nested `task` in child transcripts. | T5 | P1 | `core/subagents/*` (`subagent_proto.py`, `subagent_worker.py`, `progress_publish.py`), `_tool_task`, `subagents/worker/*`, `worker_pool.py`, `tool_batch._MAX_CONCURRENT_SUBAGENTS`. |  |

### Verifier (VER)

Existing unit coverage: `tests/unit/core/persistence`, `tests/unit/core/verifier`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-VER-01 | Write without read positive / _negative | Unread write emits `write_without_read`; read-then-write emits nothing. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-02 | Error streak positive / _negative | 3 failures: `error_streak`, status `stuck`; 2 failures: nothing. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-03 | Rewrite churn positive | Repeated rewrites of one file emit `rewrite_churn` after `min_turn_before_verdict`. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-04 | Constraint touch positive / _negative | `path_glob: db/migrations/**`: writing there emits `constraint_touch` before turn 3; writing `src/models.py` emits nothing. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-05 | Free text constraint never matches | Pins current behavior. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-06 | Done unmet positive | Done-when `DONE.txt`, turn ends without it: `done_unmet`, status `stuck`. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-07 | Repeat correction positive | Same path touched after a `correction` entry. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-08 | Preempt negative, supersession negative | `preempt` / `scope_change` intents: zero verdicts. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-09 | No progress budget burn not mailboxed | Logged only. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-10 | Min turn before verdict | Non-ledger signals wait until turn 3; ledger signals do not. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-11 | Severity cap nudge | Verdict `block` with `max_severity: nudge`: nudge text injected; tools not stripped; no `verifier_replan`. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-12 | Replan one turn | `max_severity: replan`: one `verifier_replan`; next request has no tools; the one after has tools. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-13 | Block denies mutating tools | `block` verdict current: writes denied by `VerifierInspector`; reads allowed. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-14 | Mailbox stale checkpoint dropped | Verdict for an old checkpoint is not applied. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-15 | Sticky nudge | Nudge persists across inner turns until resolved. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-16 | Verdict in history stripped for provider | `SystemNotification` mirror exists in history, absent from provider messages. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-17 | Judge replay valid | `ScriptedJudge` valid JSON: parsed status; free-form correction text dropped; only template text injected. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-18 | Judge malformed fails open | Malformed / timeout: no nudge. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-19 | Judge spend ratio | Over `max_spend_ratio`: judge not called. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-20 | Judge rate limits | `max_verdicts_per_message`, `min_turns_between_verdicts` respected. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-21 | Rewrite effects branch copy | Branch edit copies ledger rows; truncate drops them. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-22 | Verifier off no effect | All flags off: zero verifier events and identical provider requests to a verifier-less run. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T2-VER-23 | Drift confusion matrix | Aggregates VER-01..10: precision = recall = 1.0 per signal. | T2 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T3-VER-01 | Replay drift tape | Agent `ProviderEvent` tape + classifier JSON + judge JSON from one live run: same `triggering_signals` sequence and interventions. | T3 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T4-VER-01..05 | Drift/* | Split configs: `verifier-tracker.yaml` (judge off) and `verifier-judge.yaml` (judge on, `max_severity: nudge`). Report a confusion matrix (predicted × gold signal). | T4 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |
| T5-VER-01 | Trials drift per config | Gate on recall of `done_unmet` and `constraint_touch`, and on zero verdicts for `preempt` / `supersession` in ≥ k of N. Judge config must beat tracker-only recall before it ships. | T5 | P1 | `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`. |  |

### Scheduler (SCHED)

Existing unit coverage: `tests/unit/core/goals`, `tests/unit/core/persistence`, `tests/unit/gateway/sse`, `tests/unit/scheduler`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-SCHED-01 | Interval parse floor | `4s` raises; `5s` → 5000; `5m` → 300000; malformed stored intervals skipped by `list_due`. | T1 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-01 | Enable loops progressive | Loop tools absent until `enable_loops`; present on the next request; `disable_loops` removes them. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-02 | Loop lifecycle real store | `start_loop` (confirmed) → `loop_status` → `pause_loop` → `resume_loop` → `stop_loop`: row ends `completed`. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-03 | Start loop requires stop guard | No `max_ticks` / `max_runtime` and not `unbounded`: validation error. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-04 | Loop goal isolation | `pause_loop` on a goal id: `unknown loop`. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-05 | One tick fake clock | Advance clock past `next_tick_at_ms`: exactly one invoke with `format_tick_prompt`. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-06 | Busy session defers | `skip_if_busy`: busy session defers; `defer_tick` keeps a reclaimed claim. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-07 | Loop error fails goal retries | Loop tick error fails the loop; goal tick error stays active until 3 consecutive failures. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-08 | Stale claim reclaimed | Fake clock past `STALE_CLAIM_MS`: reclaimed; heartbeat prevents reclaim during a slow tick. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-09 | Create goal requires slash goal | `create_goal` without `/goal`: error; with it: one row. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-10 | One open goal per session | Second `create_goal`: `OpenGoalExistsError`. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-11 | Tool loops | As today. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. | regression/t2/loop/scenarios/scheduler/tool_loops.yaml |
| T2-SCHED-G1 | Invoke tick busy 409 | `POST /scheduler/invoke-tick` during a turn: 409 `SESSION_BUSY`; loop not failed. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T2-SCHED-G2 | Routes lifecycle | `/scheduler/loops` CRUD and `/goals` lifecycle; reserved session ids rejected; `confirmed=true` required. | T2 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T4-SCHED-01 | Http one tick live | Create via HTTP + invoke-tick: history gains one user message equal to the tick prompt; a real reply follows. | T4 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T4-SCHED-02 | Model starts loop | "Check X every 5 minutes" → `required_tools: [start_loop]` with a stop guard. | T4 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |
| T5-SCHED-01 | Trials of T4-SCHED-02 | Capability. | T5 | P1 | `scheduler/*` (`engine.py`, `worker.py`, `interval.py`), loop tools in `CoreToolExecutor`, `scheduled_loops.py`, `goals/service.py`, `gateway/sse/scheduler_routes.py`, `goal_routes.py`. |  |

### Skills (SKILL)

Existing unit coverage: `tests/unit/core/context`, `tests/unit/core/tools`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-SKILL-01 | Discover rules | Only immediate subdirs with `SKILL.md`; nested ignored; frontmatter `description` wins over first line. | T1 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T2-SKILL-01 | List then read skill | `list_skills` then `read_file skills/diagnostics/SKILL.md` returns the file. | T2 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T2-SKILL-02 | Skill write denied | Write/replace/patch under `skills/`: `skills_read_only`; bytes unchanged. | T2 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T2-SKILL-03 | Skill symlink escape | Escaping symlink under skills: `path_escape`. | T2 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T2-SKILL-04 | Skills in prompt | System prompt `## Skills` lists names + descriptions. | T2 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T2-SKILL-05 | Skills path hot reload | Changing `paths.skills_path` applies on the next turn (volatile `skills` source). | T2 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T2-SKILL-06 | Slash unknown skill | `/nope`: treated as text, no invoked section. | T2 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T4-SKILL-01 | Skill invocation | `required_tools: [list_skills, read_file]`. | T4 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |
| T5-SKILL-01 | Trials of T4-SKILL-01 | Reliability. | T5 | P1 | `context._discover_skills`, `_parse_skill_description`, `list_skills`, virtual `skills/` mount, `core/context/slash_skills.py`. Note: `src/monkeybot/skills/` contains only stale bytecode; discovery is entirely in `core/context/__init__.py`. |  |

### MCP (MCP)

Existing unit coverage: `tests/unit/core/context`, `tests/unit/core/mcp`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-MCP-01 | Enable then call | Progressive config: request 1 lacks `fixture__echo`; `enable_mcp fixture`; request 2 includes it; call returns echo; `provider_calls: 3`. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-02 | Disable removes tools | `disable_mcp`: tool gone from the next request. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-03 | Meta tools only when connected | Resource/prompt tools absent before connect, present after. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-04 | Resources and prompts | `list_mcp_resources` / `read_mcp_resource fixture://readme` / `list_mcp_prompts` / `get_mcp_prompt greet`. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-05 | Autoconnect failure is status | Broken sibling server: load returns; status failed; good server still connected. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-06 | Strict load rolls back | `MCP_STRICT_LOAD`: failure aborts and siblings roll back. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-07 | Name prefix split | Servers `a` and `a_b`: `a_b__tool` routes to `a_b`. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-08 | Env interpolation missing | `${MISSING}` becomes `""`, server still loads. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-09 | Catalog diff reload | Edit `mcp.json`: changed server reconnects; bad JSON keeps existing connections. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-10 | Enable unknown server | Not in catalog: error string (pinned; not an envelope). | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T2-MCP-11 | Disabled server not catalogued | `enabled: false`: not offered. | T2 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T3-MCP-01 | Replay tools list call | Recorded `tools/list` + `tools/call` JSON: same prefixed names and normalized result text. | T3 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T4-MCP-01..03 | Mcp/* | autoConnect path as today. | T4 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T4-MCP-04 | Enable then echo live | Progressive config: `required_tools: [enable_mcp, fixture__echo]`, `max_tool_errors: 0`. | T4 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |
| T5-MCP-01 | Trials of T4-MCP-04 | Capability. | T5 | P1 | `core/mcp/mcp_client.py`, `oauth_flow.py`, `_tool_enable_mcp` / `_tool_disable_mcp`, progressive meta-tools, `apply_catalog_diff`. |  |

### Web (WEB)

Existing unit coverage: `tests/unit/web_search`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-WEB-01 | Web search | Result JSON `ok:true`, `query` echoed, results passed through. | T2 | P2 | `web_search/*`. Browser MCP is a separate package (`integrations/browser-mcp`) with its own suite; it stays out of scope here except the harness hook. | regression/t2/loop/scenarios/web/web_search.yaml |
| T2-WEB-02 | Empty query | `ok:false`; backend not called. | T2 | P2 | `web_search/*`. Browser MCP is a separate package (`integrations/browser-mcp`) with its own suite; it stays out of scope here except the harness hook. |  |
| T2-WEB-03 | Backend none not advertised | `WEB_SEARCH_BACKEND=none`: tool absent. | T2 | P2 | `web_search/*`. Browser MCP is a separate package (`integrations/browser-mcp`) with its own suite; it stays out of scope here except the harness hook. |  |
| T2-WEB-04 | Max results | `WEB_SEARCH_MAX_RESULTS=2`: two results. | T2 | P2 | `web_search/*`. Browser MCP is a separate package (`integrations/browser-mcp`) with its own suite; it stays out of scope here except the harness hook. |  |
| T2-WEB-05 | Browser stop on disconnect | Disconnecting a server named `browser` calls `browser_stop` first (harness side only). | T2 | P2 | `web_search/*`. Browser MCP is a separate package (`integrations/browser-mcp`) with its own suite; it stays out of scope here except the harness hook. |  |

### Memory (MEM)

Existing unit coverage: `tests/unit/core/memory`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-MEM-01 | Share threads flag | `MONKEYBOT_MEMORY_SHARE_THREADS` off: thread B recall excludes thread A; on: includes. | T1 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T1-MEM-02 | Drain timeout keeps pending | Hung embedder: rows stay `pending` after `drain_writer`. | T1 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T1-MEM-03 | Object store uri fallback | `gcs://` / `s3://` memory URI falls back to local; `palace_path_from_uri` raises for object stores. | T1 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-01 | Outbox ingest then inject | Turn 1 persists; `drain_writer`; turn 2 `PRE_TURN` injects the turn-1 text (content check). | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-02 | Outbox thread isolation | Thread B never sees thread A's lines with share-threads off. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-03 | Ambiguous commit no duplicate | Simulated `AmbiguousCommitError`: retry once, one history row, one outbox row. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-04 | Outbox failure does not fail turn | Outbox write fails after history commit: turn completes; history-only. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-05 | Memory off | `memory.enabled: false`: no wake-up block, `mempalace search` refused. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-06 | Mempalace search real | Seeded `InMemoryPalace` behind the search route: stdout contains the drawer; other thread's drawer absent. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. | regression/t2/loop/scenarios/memory/mempalace_search_real.yaml |
| T2-MEM-07 | Subagent no ingest | Child turn creates no outbox rows. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T2-MEM-08 | Memory write inject | As today, with content checks. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. | regression/t2/loop/scenarios/memory/memory_write_inject.yaml |
| T2-MEM-09 | Ephemeral palace shared outbox rejected | Postgres/Firestore shared outbox + ephemeral palace: `MemoryConfigurationError` at boot. | T2 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T3-MEM-01 | Replay wake up | Recorded `load_index()` lines for a palace snapshot match byte for byte. | T3 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T4-MEM-01..02 | Memory/recall_* | Plus a sidecar check that the drawer was filed under session 1's thread. | T4 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T4-MEM-03 | Search past session | Session 2 calls `mempalace search` and answers with the session-1 fact. | T4 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |
| T5-MEM-01 | Trials memory | Capability; tool-call rate for `mempalace` reported. | T5 | P1 | `core/memory/*` (`subsystem.py`, `hook.py`, `ingest.py`, `writer.py`, `palace.py`, `outbox.py`, `postgres.py`, `firestore.py`, `migrate_embedding.py`, `migrate_layout.py`). The only agent-facing memory tool is `run_command mempalace search`; there are no search/edit/update/forget tools. |  |

### Persistence (STORE)

Existing unit coverage: `tests/unit/core/persistence`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-STORE-01 | History roundtrip scope idempotent | Two scopes; same `message_id` twice → one row; other scope's `list_threads` empty. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-02 | Reset preserves order and row ids |  | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-03 | Delete rows legacy and stored | `legacy:<key>` and real ids. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-04 | Hidden threads | `subagent:` and branch threads absent from `list_threads`. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-05 | Append with outbox atomic | Outbox failure rolls back the history row. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-06 | Branches one active | Concurrent creates leave one active branch. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-07 | Branch ops parity | Edit / rewind / fork / point-survives-compaction on every backend. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-08 | Usage breakdown buckets | hour/day/week keys match `utc_bucket_key`. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-09 | Run claim single winner stale reset |  | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-10 | Loop claim defer complete |  | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-11 | Turn lock exclusive and stale |  | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-12 | Outbox claim scoped | Other `palace_id` / `agent_id` not claimed; gc respects age. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-13 | Goal ledger presence | SQLite: `commit_classified` rolls back on failure. Postgres/Firestore: `None` (pinned until implemented). | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-14 | Known unscoped tables | Pins today: usage/runs/loops/locks visible across agent scopes on a shared DB. Flip when scoping lands. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-20 | Legacy db apply schema | Each `fixtures/legacy_db/*.sqlite` through `apply_schema`: columns present, row counts kept, legacy rows load as `legacy:<key>`. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-21 | Create schema matches apply schema | `xfail(strict=True)` today: `scripts/create_schema.py` misses the `_ensure_*` upgrades and branch rebuild. Turns green when fixed. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-22 | Auto schema false | `paths.auto_schema: false` skips DDL; missing `memory_outbox` fails ready. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-23 | Embedding migrate lock | Second migrator skips while the lock is held; explicit Gemma palaces untouched. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-24 | Layout migrate nonempty dest | Non-empty destination not merged. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T1-STORE-25 | Firestore summary doc migration | Old summary doc removed, scoped doc written. | T1 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T2-STORE-01 | Compaction reset row ids | After a scripted compaction, `load` has the summary row and preserved tail `row_id`s. | T2 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T2-STORE-02 | Usage recorded on turn complete | One `turn_usage` row per user message matching `TurnComplete.usage`. | T2 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T2-STORE-03 | Transcript convertible | `runtime.transcript_enabled: true` run produces a transcript that `from_transcript.py` converts into a cassette that replays green (closes the T3 loop). | T2 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |
| T2-STORE-G1 | Rewrite regenerate replays tool safe | Regenerate across a tool pair: `HistoryRewritten` then replay turn; tool pairs intact. | T2 | P0 | `core/persistence/*` (history, branches, row ids, usage, durable runs, scheduled loops, goal ledger, session turn locks, transcripts, `sqlite.py` / Postgres / Firestore backends), `backends.py`, `scripts/create_schema.py`, `docs/migrations/*`. |  |

### Config (CFG)

Existing unit coverage: `tests/unit/core`, `tests/unit/core/config`, `tests/unit/gateway/sse`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-CFG-01 | Precedence matrix | Table: env vs YAML vs `includes` vs `MONKEYBOT_CONFIG`; `model.*` YAML-only; leftover `MODEL_PROVIDER` without YAML raises. | T1 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T1-CFG-02 | Every env key has tier | Every `ENV_MAP` key maps to exactly one `ConfigTier`; a new key without a tier fails. | T1 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T1-CFG-03 | Retired sections warned | `knowledge:`, `context_curation:` warned and ignored. | T1 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T1-CFG-04 | Yaml validation rejects | `memory.backend: s3`, unknown provider ids, `MODEL_PROVIDER=fake` via `get_provider_config`. | T1 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T1-CFG-05 | Provider aliases | `gemini`/`vertex` → `google_vertexai`, `vertex-claude` → `vertex_anthropic`, `ollama_cloud` → `ollama-cloud`. | T1 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T1-CFG-06 | Relative paths anchor agent root | `sqlite:///x.db`, memory URI resolve against the agent root. | T1 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T2-CFG-G1 | Reload hot key | `POST /admin/config/reload` changing `model.name`: `ConfigReloaded` on every open session; next turn uses the new model. | T2 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T2-CFG-G2 | Reload restart key reported | `DB_URL` change: `restart_required` lists it; backend unchanged. | T2 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T2-CFG-G3 | Reload mid turn pinned | Reload during a turn: in-flight turn keeps the pinned revision. | T2 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T2-CFG-G4 | Reload auth | Non-loopback rejected; bearer required when `MONKEYBOT_ADMIN_TOKEN` set. | T2 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T2-CFG-G5 | Bootstrap memory failure continues | Ordinary memory setup failure: gateway ready without memory; `MemoryConfigurationError` aborts. | T2 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |
| T4-CFG-01 | Reload memory off live | Reload `memory.enabled: false`; next live turn has no wake-up block. | T4 | P1 | `core/config/*` (`settings.py`, `runtime_env.py`, `snapshot.py`, `validation.py`), `core/bootstrap.py`, `gateway/sse/reload.py`. |  |

### Todo (TODO)

Existing unit coverage: `tests/unit/todo_list`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T2-TODO-01 | Todo list / todo list steps | List state after the turn matches the scripted calls. | T2 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). | regression/t2/loop/scenarios/todo/todo_list.yaml, regression/t2/loop/scenarios/todo/todo_list_steps.yaml |
| T2-TODO-02 | Single in progress | Setting a second item `in_progress` demotes or rejects per current rule. | T2 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |
| T2-TODO-03 | Caps | 51st item / 501-char item rejected. | T2 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |
| T2-TODO-04 | Ids stable | `tN` ids survive clear/remove/hydrate. | T2 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |
| T2-TODO-05 | Reminder once | Stale-list reminder fires once per store instance until the list changes. | T2 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |
| T2-TODO-06 | Mirror hydrate | `mirror_to_disk`: `todos.json` written; new store hydrates same ids and one `in_progress`. | T2 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |
| T4-TODO-01 | Multi turn/task tracking | Plus: `todos.json` matches the tool trace. | T4 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |
| T5-TODO-01 | Trials of T4-TODO-01 | Reliability on "exactly one in_progress at any time". | T5 | P1 | `todo_list/*` (`store.py`, `tool.py`, reminder). |  |

### Providers (PROV)

Existing unit coverage: `tests/unit/providers`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-PROV-01 | Converter conformance | Canonical conversation (system, user, assistant text + 2 parallel `ToolRequest`s, `ToolResponse` with text, image, PDF, `Thinking` with signature) through `messages_to_openai`, `build_anthropic_messages`, `_messages_to_contents`, `messages_to_converse`: roles ordered; tool results paired by id; thinking replayed or dropped as documented per adapter; media in the vendor slot or a documented placeholder. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-02 | Tool arg fragment conformance | Same fragment list (valid object, empty, truncated, JSON array, split mid-string) through Anthropic's buffer, Converse `_ToolBuf`, OpenAI slots, Gemini `_merge_function_call_args`: identical `(args, parse_error)`. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-03 | Stream order contract | Per adapter, the documented event order (text/thinking/tool/usage/done) is asserted, so a reorder is a visible change. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-04 | Usage semantics | OpenAI `input = prompt - cached`; Anthropic cached = read + creation; Gemini omits usage when missing (pinned); zero-usage event shapes. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-05 | Error mapping table | Rate limit → `ProviderRateLimitError` after 3; 5xx → `ProviderServerError`; tool-unsupported → one tool-less retry; Ollama body-read 400 → media-stripped retry; **no retry after the first yielded event**. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-06 | Sampling strip retry | `temperature` / `thinking` / `output_config` stripped on blame; no retry after content starts. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-07 | Token count fallbacks | Vertex/Bedrock fall back to estimates when the API refuses; compat providers use tiktoken (pinned). | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-08 | Cache hints | `cache_retention` none/short/long → Anthropic markers / OpenAI `prompt_cache_retention="24h"` / nothing for Gemini; session affinity header for OpenAI and Claude only. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-09 | Compat providers stream | NVIDIA, OpenRouter, Fireworks, HuggingFace each drive `Provider.stream` through a fake chunk iterator (today only constructors are tested). | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T1-PROV-10 | Request byte budget | Media trimmed before PDF extraction; `RequestByteBudgetError` when text alone exceeds the cap. | T1 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-01 | Openai tool split with and without index | Wire cassette. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-02 | Nvidia hermes tool call split | `<tool_call>` across SSE data lines. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-03 | Anthropic input json and signature | `input_json_delta` + `signature_delta` + `max_tokens` stop. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-04 | Gemini partial args thought signature no usage | Second-choice replay path. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-05 | Converse tool input string and object | Second-choice replay path. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-06 | Thinking only completion per family | Each family yields thinking then `Done` with no text. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-07 | Ollama cloud glm tool turn | The model every live run uses; recorded with `OLLAMA_API_KEY`. Also covers the shared `_openai_compat` parser. First T3b cassette to record. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T3-PROV-08 | Staleness check | Installed SDK versions match cassette headers or a re-record exists. | T3 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |
| T4-PROV-01 | Provider live matrix | Ollama Cloud always (runs first in every live suite): one `stream` with a trivial tool + `count_input_tokens`; sequence type-checks. Any other provider only if its key is set; otherwise skipped with the reason. | T4 | P0 | `providers/*` (Gemini, Claude, Vertex Claude, Bedrock Anthropic + Converse, OpenAI, Ollama, HuggingFace, NVIDIA, OpenRouter, Fireworks, Gemini Live), `_utils.py`, `_sdk_client.py`, `core/llm/provider.py`. |  |

### Gateway (GW)

Existing unit coverage: `tests/unit/core/runtime`, `tests/unit/gateway`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-GW-01 | Event to json schema | Every kind validates; no `chat_request_id`; `TurnComplete.usage` has 8 keys. | T1 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T1-GW-02 | Lossy fields pinned | `ToolCallStarted` omits `inspector_decision`/`resource`/`resolved_path`; `ToolCallResult` omits `error_kind`/`duration_ms`; `FileBlock` never has `data`; `SystemContextUpdated` omits `text`. | T1 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T1-GW-03 | Framing | `ActiveRequests` and pings have no `id:`; garbage `Last-Event-ID` → full replay. | T1 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T1-GW-04 | Verdict wire snake case | SSE `VerifierVerdict` is snake_case (not `to_wire()` camelCase). | T1 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T1-GW-05 | Route table snapshot | `app.routes` method+path set equals a committed list (Appendix D); a new/removed route is a reviewed diff. | T1 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T1-GW-06 | Error envelope | Every `APIError` code in routes has the `{error:{code,message,request_id}}` shape. | T1 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-01 | Scripted turn golden sse | Gateway driver, scripted provider: frames equal `t3/sse/basic_turn.sse`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-02 | Tool turn golden sse | Tool call + result: equals `t3/sse/tool_turn.sse`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-03 | Setup failure releases lock | Broken MCP/provider: `Error` then empty `TurnComplete`; user prompt persisted; next `/reply` not 409. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-04 | Reply busy 409 | Second `/reply` during a turn: 409 `SESSION_BUSY`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-05 | Reconnect last event id | Reconnect with `Last-Event-ID: 2`: frames 1–2 skipped, rest replayed, fresh `ActiveRequests`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-06 | Replay overflow gap | Past `SSE_REPLAY_MAX`: reconnect does not invent ids; gap documented (future `ReplayGap` event). | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-07 | Nested lane flood | > 256 nested subagent frames: parent `TurnComplete` still replayable. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-08 | Session isolation | Session B never sees A's `request_id`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-09 | Cancel during confirmation | Cancel: terminal event; later confirm POST → 409 `STALE_PENDING_RESPONSE`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-10 | Steer flow | `/steer`: `QueuedInputAccepted(queue: steer)` then `UserSteered` with in-flight `request_id`; idle → 409 `SESSION_IDLE`; full → 429. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-11 | Queue flow | `/queue` idle starts now (`position 0`); busy enqueues; promote sets `queued_request_id`; `DELETE` drops. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-12 | Ask user route | `AskUserRequest` → `POST /ask-user/{id}` → turn resumes; empty answer → 400 `EMPTY_ANSWER`. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-13 | Elicitation and frontend tool routes | `ActionRequiredEvent` / `FrontendToolRequest` round trips. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-14 | History rewrite order | `HistoryRewritten` seq < first delta of the replay turn; voice and rewrite exclude each other. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-15 | Branches routes | edit / regenerate / restore / switch / truncate / fork behaviors and 409s. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-16 | Delete session quiesce | `DELETE /sessions/{id}` during a turn: quiesces then cancels; idempotent. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-17 | Usage routes | `/sessions/{id}/usage` and `/usage?bucket=` match recorded rows; hour bucket needs `since` ≤ 7 days. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-18 | Workspace and history api toggles | `MONKEYBOT_WORKSPACE_API` / `MONKEYBOT_CHAT_HISTORY_API` off → 404; workspace reads path-confined. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-19 | Chat title route | Title ≤ 60 chars; model failure → `{title: null}`, never 500. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-20 | Health | `/health` shape and memory detail. | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T2-GW-21 | Slow subscriber | Non-reading subscriber: producer still finishes (documents unbounded queue growth). | T2 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T3-GW-01 | Trajectory cassette to golden sse | A T3a cassette through the gateway driver produces the committed golden stream. | T3 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T3-GW-02 | Fake gateway validates | Runs in monkeyapp CI against vendored schema. Fails today on `TurnComplete` without `usage` and `ContextSummarized.estimated_tokens`. | T3 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T3-GW-03 | Dispatch cases subset | Every `dispatchGatewayEvent` case is a schema type. | T3 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T4-GW-01 | One turn complete per request | Every live scenario (global live invariant). | T4 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T4-GW-02 | Live resume | Drop the client mid-delta, reconnect with `Last-Event-ID`; concatenated deltas equal the final text. | T4 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T5-GW-01 | Cancel storm | Each trial: one terminal event, lock released. | T5 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |
| T5-GW-02 | Reconnect storm | No duplicated `TurnComplete`, no lost `call_id` pairing. | T5 | P0 | `gateway/sse/*` (`routes.py`, `session_bus.py`, `sse.py`, `app.py`, `history_routes.py`, `reply_body.py`, `reload.py`), `core/runtime/events.py` (`event_to_json`). |  |

### Realtime (RT)

Existing unit coverage: `tests/unit/core/runtime`, `tests/unit/core/testing`, `tests/unit/gateway/realtime`, `tests/unit/providers`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-RT-01 | Divergence pinned | Realtime loop does not import / call the text-loop guards; shares truncated-batch rejection, confirm wait, registry refresh. A refactor that changes this is a reviewed diff. | T1 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-01 | Ws text turn | WS `connect` → `text` → `text_delta`s → `turn_boundary`; `usage` frame. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-02 | Ws tool turn | `tool_call` → `tool_result` → continue. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-03 | Ws tool confirmation | `tool_confirmation` frame → `tool_confirmation_response`. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-04 | Ws interrupt | `interrupt` → `interrupted`; partial assistant committed. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-05 | Ws slot limit | Over the slot limit: upgrade rejected before accept. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-06 | Ws guardrail budget | Budget exceeded: `session_ended` with reason. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T2-RT-07 | Ws blocks history rewrite | Live voice session: `DELETE /api/chat-history/{id}` → 409. | T2 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |
| T4-RT-01 | Gemini live smoke | Requires GCP credentials; not runnable with the Ollama-only live setup (5.11). Listed under Deferred coverage in `docs/testing.md`. | T4 | P1 | `core/runtime/realtime_loop.py`, `utterance_buffer.py`, `gateway/realtime/*`, `providers/gemini_live.py`, `core/testing/mocks_realtime_provider.py`. |  |

### Ops (OPS)

Existing unit coverage: `cli/tests`, `tests/unit/cli`, `tests/unit/observability`, `tests/unit/packaging`.

| ID | Case | Expect | Tier | Pri | Origin | Test |
| --- | --- | --- | --- | --- | --- | --- |
| T1-OPS-01 | Scaffold reexports | Names re-exported by `monkeybot.scaffold` exist in `monkeybot_cli.scaffold`. | T1 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T1-OPS-02 | Span names contract | Span names and required attributes for run/turn/llm/tool/summarize/subagent match a committed list. | T1 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T2-OPS-01 | Cli run boots | `monkeybot run` subprocess with `MODEL_PROVIDER=fake`: `/health` 200, `/realtime/health` 200; SIGINT exits 130. | T2 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T2-OPS-02 | Cli new then validate | `monkeybot new` → `monkeybot validate` passes on the scaffold. | T2 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T2-OPS-03 | Cli chat against gateway | `monkeybot chat` one message against the in-process gateway prints the scripted reply. | T2 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T2-OPS-04 | Otel span tree | `MONKEYBOT_OTEL_ENABLED=1`, sqlite exporter, scripted tool turn: span tree run → turn → llm + tool; `TurnComplete.trace_id` equals the root trace id; secrets truncated/denylisted. | T2 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T2-OPS-05 | Traceparent to subagent | `task` envelope carries `traceparent`; child spans share the trace id. | T2 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T2-OPS-06 | Transcript manifest | Transcript has `SessionManifest`, provider request/response records, no deltas. | T2 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |
| T4-OPS-01 | Docker image health | Built image answers `/health` on 8080. | T4 | P2 | `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`. |  |

## 9. Deferred coverage

- Computer-use tools run on macOS. They stay out of the Linux CI job (`darwin_only`).
- Postgres and Firestore parity runs in the optional `storage-parity` job, outside the push gate.
- `integrations/browser-mcp` keeps its own suite. Its internals are outside this catalog.
- Production trace mining is out of scope.
- Performance measurement stays in `profiling/`.
