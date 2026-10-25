# Phase 6 — Cutover (≈ 1 day)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

- Delete `evals/`, `tests/evals/`, and the now-empty `tests/integration/`, `tests/acceptance/`, `tests/profiling/` (section 4.5); update `docs/live-evals.md` (now a T4 runbook linking to `docs/testing.md`), `CONTRIBUTING.md` (points to `docs/testing.md` and the failure policy), the workflow, `ci-local.sh`.

Exit: no references to the old paths (`rg "evals/|tests/evals|tests/core/|from tests\."` clean outside history docs); `docs/testing.md` has no empty Test cells except rows listed under §9 Deferred coverage.

## Tasks

### P6-01 — Cutover

- **Depends on:** every Phase 1–3 task `done` or `deferred` in `status.md`, and P5-03. For a partial cutover, P4-06 is enough instead of P5-03, provided the Phase 5 tasks are marked `deferred`.
- **Blocked by:** none
- **Read:** [07-migration-map.md](../reference/07-migration-map.md) (all); [04-file-structure.md](../reference/04-file-structure.md) §4.5 and §4.6; this phase file's Summary
- **Creates / changes:**
  - deletes what is left of `evals/` (`knowledge_layer/`, `runs/`, `monkeybot_evals.egg-info/`, `TEST_COVERAGE.md`, empty folders), `tests/evals/` if still present, the empty `tests/integration/`, `tests/acceptance/`, `tests/profiling/`, and any leftover shims;
  - `docs/live-evals.md`: becomes the T4 runbook, with new paths, `OLLAMA_API_KEY`, and a link to `docs/testing.md`;
  - `README.md`, `CONTRIBUTING.md` and `docs/testing.md` §1–§3, for the final layout and the failure policy;
  - `.gitignore`: drop the `/evals/...` entries;
  - `.github/workflows/ci.yml` and `scripts/ci-local.sh`: no `evals/` paths left;
  - the `.cursor/rules` files that mention old paths.
- **Steps:**
  1. Run `rg -n 'evals/|tests/evals|tests/core/|from tests\.' --glob '!regression/plan/**' --glob '!CHANGELOG*'`. Fix every hit, or list it in the report with the reason it stays (for example a dated history doc).
  2. Run the pairing test. Empty Test cells are allowed only for deferred cases: T4-RT-01, T5-GW-* if Phase 5 is deferred, and the D3- and D13-blocked cases. List each one in `docs/testing.md` §9 (Deferred coverage) with its reason. Do not write the reason into the catalog's Expect column.
  3. Run the full gate set, plus one live smoke (confirmation needed).
- **Verify:** `bash scripts/ci-local.sh`; `uv run pytest tests regression -m "t1 or t2 or t3" -q -n auto`; `uv run pytest tests/contracts/test_catalog_pairing.py -q`
- **Done when:** the Summary exit criteria hold, and `status.md` shows every task `done` or `deferred`.
