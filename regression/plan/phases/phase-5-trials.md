# Phase 5 — Trials and baselines (≈ 2–3 days)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

- `t5/trials.py` with Wilson intervals, reliability vs capability classes, keyed baselines.
- First baselines for the T4 suites `smoke`, `coding`, `coding_hard`, `drift` (both verifier configs), `subagents`, `memory`, `mcp`.
- Nightly / manual workflow.

Exit: baselines committed; release-gate job compares against them; a regressed trial verdict produces a ledger record with `trials` k/N and baseline k/N.

## Tasks

Order: P5-01 → P5-02 → P5-03.

Terms used in this phase:

- A **trial run** repeats one T4 suite (`regression/t4/suites/<suite>.yaml`) N times: `uv run python -m regression.t5 --suite <suite> --trials N`.
- `regression/t5/suites/{reliability,capability,drift}.yaml` are **class lists**, not runnable suites. They say which scenarios are scored pass^N (reliability) and which get a Wilson interval (capability), and they carry the T5 case IDs.
- Baselines are keyed by T4 suite, plus the verifier config for `drift`: `regression/t5/baselines/<suite>[@<config>]__ollama-cloud__glm-5.3-flash.json`. For example `coding_hard__ollama-cloud__glm-5.3-flash.json` and `drift@verifier-judge__ollama-cloud__glm-5.3-flash.json`.
- `regression/t4/baselines/smoke.json` is unrelated: it is the T4 report's token and latency baseline.

---

### P5-01 — Statistics, comparison, class lists

- **Depends on:** P4-06
- **Blocked by:** none
- **Read:** [05.6-pytest-reporting-trials.md](../reference/05.6-pytest-reporting-trials.md) §5.8; [03-tiers.md](../reference/03-tiers.md), the T5 row; [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t5/` subtree only; the T5 rows of every area file (`rg -n '^\| T5-' ../reference/06-coverage/`); `regression/t5/trials.py`, moved in P4-01
- **Creates / changes:** `regression/t5/{__main__,stats,compare}.py`; `regression/t5/suites/{reliability,capability,drift}.yaml`, which list each T5 case ID with its scenarios and become those cases' Test cells; `regression/selftest/test_trials_stats.py`; `trials.py` reports through the ledger
- **Steps:**
  1. `stats.py` computes pass^N for reliability scenarios and a Wilson interval for capability scenarios, with the fail and warn rules of §5.8.
  2. `compare.py` looks up the baseline by the key above. A missing baseline is reported as "no baseline", not as a pass.
  3. A `fail` verdict writes a ledger failure with trial k/N and baseline k/N; a `warn` verdict writes one with `severity: warn`.
  4. `--update-baseline` writes the baseline file; nothing else does (§5.8).
- **Verify:** `uv run pytest regression/selftest/test_trials_stats.py -q`, checked against hand-computed values and a synthetic regressed baseline.
- **Done when:** the comparison and the ledger records are tested without a live run, and every T5 row except T5-GW-01, 02 has a Test cell.

### P5-02 — First baselines (live, takes hours)

- **Depends on:** P5-01
- **Blocked by:** user confirmation (time and Ollama usage); D12, for judged suites
- **Read:** [05.11-live-provider.md](../reference/05.11-live-provider.md), the "Rate and cost" paragraph; [05.6-pytest-reporting-trials.md](../reference/05.6-pytest-reporting-trials.md) §5.8
- **Creates:** `regression/t5/baselines/<suite>[@<config>]__ollama-cloud__glm-5.3-flash.json` for each suite in the Summary
- **Steps:**
  1. Agree N with the user (the §5.8 defaults are 8 for capability and 5 for reliability).
  2. Run one suite per session with `--update-baseline`, keeping to rate limits.
  3. Show each baseline and its scorecard for review. Baselines are committed in their own PR (§5.8), like goldens.
- **Verify:** a second, short trial run (N = 2) of `smoke` compares against the new baseline and reports a verdict, not "no baseline".
- **Stop and ask if:** Ollama rate limits stop a suite. Do not switch providers, because the baselines are keyed to this one.

### P5-03 — Nightly workflow, release-gate compare, gateway storms (T5-GW-01, 02)

- **Depends on:** P5-02, P2-01 (the storm tests use `testkit.gateway` and `testkit.sse`)
- **Blocked by:** D1, for running in CI
- **Read:** [09-ci.md](../reference/09-ci.md), the `live-trials` row; [6U-gw.md](../reference/06-coverage/6U-gw.md), the T5 rows; [05.1-doubles.md](../reference/05.1-doubles.md), the gateway-harness row
- **Creates:**
  - `.github/workflows/live-trials.yml`: nightly and manual. On pull requests into `main` (release PRs) it runs `python -m regression.t5` against the committed baselines and fails on a `fail` verdict; that is the release-gate compare;
  - `regression/t5/python/test_cancel_storm.py`, `regression/t5/python/test_reconnect_storm.py` (in-process gateway, scripted provider; `t5` and `slow` markers).
- **Verify:** `uv run pytest regression/t5/python --live -q`. `--live` is needed only because every `t5` item is gated by it; these tests start no agent and need no key or network, and the preflight check runs only before T4 scenarios. Also run `rg -n 'evals/' .github/workflows/live-trials.yml` finds nothing.
- **Done when:** the phase Summary exit criteria hold (the CI parts pending D1).
