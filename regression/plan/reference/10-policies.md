# 10. Policies

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


## 10.1 Bug → regression case

Every bug fix PR adds at least one case that fails before the fix and passes after, at the lowest tier that can express it (T1 > T2 > T3; T4 only if the bug is about model behavior). The case sets `regression_of:` to the commit or PR. Reviewers check for it.

## 10.2 Goldens

`--update-golden` rewrites goldens locally and refuses to run when `CI` is set. Golden diffs (prompt text, SSE schema, route table, span names) are reviewed like code.

## 10.3 Cassettes

Re-record only with `MONKEYBOT_RECORD_CASSETTES=1` and a scrub pass. A re-record PR contains only cassette changes plus the reason. Strict replay failures (`cassette_stale`) are fixed by re-recording, not by loosening the replay.

## 10.4 Flakes

T1–T3 have zero flake tolerance. A flaky deterministic test is a bug (usually a missing fake clock or settlement wait). Temporary `quarantine` marker requires an issue link and an expiry date; the collector fails on expired quarantines. T4 flakiness is handled by T5, not by retries.

## 10.5 Naming

Scenario file name = case slug (`doom_identical_read.yaml`). The case ID (`T2-LOOP-07`) lives in the YAML `case` field (or `@pytest.mark.case`) and in the `docs/testing.md` catalog. IDs are never reused.

## 10.6 Failures found while running tests

- A failure is recorded (5.9) and reported, not fixed in passing. This applies to people too: a failure unrelated to your change gets a ledger fingerprint in the PR description or an issue, not a drive-by edit.
- Fixing is a separate, deliberate task that starts from `python -m testkit.failures prompt <fingerprint>` and ends with `mark <fingerprint> --status fixed`.
- The fix changes the code under test. Changing the assertion is allowed only when the catalog Expect cell is wrong, and then the Expect cell changes first, in the same PR (5.10).
- `--no-verify`, `--no-ledger`, new `skip`/`xfail`/`quarantine` markers, and deleted tests are never a way to get past a failure.
