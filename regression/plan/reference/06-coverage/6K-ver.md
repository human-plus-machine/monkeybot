# 6.K Verifier: ledger, tracker, judge, actuation (`VER`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/verifier/*`, `core/persistence/goal_ledger.py`, gateway wiring in `gateway/sse/app.py`. Design: `docs/verifier-agent-design.md`.

Existing: T1 `test_goal_ledger.py`, `test_verifier_tracker.py`, `test_progress_tracker.py`, `test_verifier_sticky_nudge.py`, `test_verifier_mailbox_lifecycle.py`, `test_verifier_inspector.py`, `test_verifier_binding.py`, `test_verifier_rewrite_effects.py`, `test_verifier_judge.py`. T2: `tests/evals/test_verifier_port.py` (one `ScriptedVerifier` unit). T4 `drift` suite (5 scenarios), not gated. The shipped `monkeybot.verifier-on.yaml` has `judge.enabled: false` and `max_severity: none`, so live drift runs measure tracker verdicts only.

**Deterministic precision/recall.** Each T2 drift scenario labels its steps as positive (a named signal must appear) or negative (no verdict, or a verdict without that signal). `ScriptedClassifier` fixes the ledger; the judge is off so `_maybe_emit` is the verdict source. A dedicated test aggregates all drift scenarios into a per-signal confusion table and asserts precision = 1.0 and recall = 1.0 on the scripted corpus (it is deterministic, so anything less is a regression). `no_progress` and `budget_burn` are always negatives on the mailbox.

| ID | Case | Asserts |
|---|---|---|
| T2-VER-01 | `write_without_read_positive` / `_negative` | Unread write emits `write_without_read`; read-then-write emits nothing. |
| T2-VER-02 | `error_streak_positive` / `_negative` | 3 failures: `error_streak`, status `stuck`; 2 failures: nothing. |
| T2-VER-03 | `rewrite_churn_positive` | Repeated rewrites of one file emit `rewrite_churn` after `min_turn_before_verdict`. |
| T2-VER-04 | `constraint_touch_positive` / `_negative` | `path_glob: db/migrations/**`: writing there emits `constraint_touch` before turn 3; writing `src/models.py` emits nothing. |
| T2-VER-05 | `free_text_constraint_never_matches` | Pins current behavior. |
| T2-VER-06 | `done_unmet_positive` | Done-when `DONE.txt`, turn ends without it: `done_unmet`, status `stuck`. |
| T2-VER-07 | `repeat_correction_positive` | Same path touched after a `correction` entry. |
| T2-VER-08 | `preempt_negative`, `supersession_negative` | `preempt` / `scope_change` intents: zero verdicts. |
| T2-VER-09 | `no_progress_budget_burn_not_mailboxed` | Logged only. |
| T2-VER-10 | `min_turn_before_verdict` | Non-ledger signals wait until turn 3; ledger signals do not. |
| T2-VER-11 | `severity_cap_nudge` | Verdict `block` with `max_severity: nudge`: nudge text injected; tools not stripped; no `verifier_replan`. |
| T2-VER-12 | `replan_one_turn` | `max_severity: replan`: one `verifier_replan`; next request has no tools; the one after has tools. |
| T2-VER-13 | `block_denies_mutating_tools` | `block` verdict current: writes denied by `VerifierInspector`; reads allowed. |
| T2-VER-14 | `mailbox_stale_checkpoint_dropped` | Verdict for an old checkpoint is not applied. |
| T2-VER-15 | `sticky_nudge` | Nudge persists across inner turns until resolved. |
| T2-VER-16 | `verdict_in_history_stripped_for_provider` | `SystemNotification` mirror exists in history, absent from provider messages. |
| T2-VER-17 | `judge_replay_valid` | `ScriptedJudge` valid JSON: parsed status; free-form correction text dropped; only template text injected. |
| T2-VER-18 | `judge_malformed_fails_open` | Malformed / timeout: no nudge. |
| T2-VER-19 | `judge_spend_ratio` | Over `max_spend_ratio`: judge not called. |
| T2-VER-20 | `judge_rate_limits` | `max_verdicts_per_message`, `min_turns_between_verdicts` respected. |
| T2-VER-21 | `rewrite_effects_branch_copy` | Branch edit copies ledger rows; truncate drops them. |
| T2-VER-22 | `verifier_off_no_effect` | All flags off: zero verifier events and identical provider requests to a verifier-less run. |
| T2-VER-23 | `drift_confusion_matrix` (Python) | Aggregates VER-01..10: precision = recall = 1.0 per signal. |
| T3-VER-01 | `replay_drift_tape` | Agent `ProviderEvent` tape + classifier JSON + judge JSON from one live run: same `triggering_signals` sequence and interventions. |
| T4-VER-01..05 | `drift/*` (migrate) | Split configs: `verifier-tracker.yaml` (judge off) and `verifier-judge.yaml` (judge on, `max_severity: nudge`). Report a confusion matrix (predicted × gold signal). |
| T5-VER-01 | trials `drift` per config | Gate on recall of `done_unmet` and `constraint_touch`, and on zero verdicts for `preempt` / `supersession` in ≥ k of N. Judge config must beat tracker-only recall before it ships. |
