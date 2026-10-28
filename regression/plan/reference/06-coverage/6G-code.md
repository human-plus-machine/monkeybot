# 6.G Coding mode: read ledger, verification gate, git context (`CODE`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/tools/read_ledger.py`, `core/coding/verification_gate.py`, `git_context.py`, `ReadLedgerCompactionHook`.

Existing: T1 `test_verification_gate.py` (+3 `run()` tests), `test_project_context.py`. T2: `coding_read_before_write`, `coding_gate_rejects_unverified`, `coding_gate_passes_when_verified`, `coding_fix_failing_test` (only real-executor scenarios today). T4 `coding` (3) and `coding_hard` (7) suites, not in CI.

| ID | Case | Asserts |
|---|---|---|
| T2-CODE-01 | `read_before_write` (migrate) | `read_before_write` code; pytest result as today. |
| T2-CODE-02 | `gate_rejects_unverified` (migrate) | `interventions: [final_answer_gate]`, `provider_calls: 5`. |
| T2-CODE-03 | `gate_passes_when_verified` (migrate) | No intervention. |
| T2-CODE-04 | `fix_failing_test` (migrate) | `workspace_command_passes`. |
| T2-CODE-05 | `gate_masked_exit` | `pytest \| tee x` does not count as verification. `pytest && true` does. |
| T2-CODE-06 | `gate_non_executing_wrapper` | `cat pytest.log`, `echo pytest`, `git log` do not count. |
| T2-CODE-07 | `gate_background_verify_not_counted` | `pytest` with `background:true` does not clear the gate. |
| T2-CODE-08 | `gate_write_after_verify` | Verify, then another write: gate holds again. |
| T2-CODE-09 | `gate_open_todos_reject` | Todo `in_progress` touched this message: rejected; untouched todos do not gate. |
| T2-CODE-10 | `gate_question_not_gated` | Pure question with no writes: no gate. |
| T2-CODE-11 | `gate_cap_then_accept` | `max_rejections: 1`: second final answer accepted and streamed. |
| T2-CODE-12 | `gate_custom_verify_commands` | `coding.verification_gate.verify_commands: ["just check"]` counts. |
| T2-CODE-13 | `gate_rejected_answer_not_persisted` | Rejected text is not in history. |
| T2-CODE-14 | `git_context_note` | Dirty fixture repo: `git: <branch>` line in `## Runtime notes`. |
| T2-CODE-15 | `git_context_absent` | Non-repo / `git` missing / timeout: no note, no error. |
| T2-CODE-16 | `git_env_isolation` | `GIT_DIR` in the environment does not leak into the status call. |
| T2-CODE-17 | `compaction_forgets_reads` | After compaction, editing a previously read file returns `read_before_write`. |
| T2-CODE-18 | `subagent_has_ledger_no_gate` | Child edits are ledger-gated; child final answer is not gate-held. |
| T4-CODE-01..03 | `coding/*` (migrate) | As today. |
| T4-CODE-04..10 | `coding_hard/*` (migrate) | As today, oracle self-test kept. |
| T5-CODE-01 | trials `coding` + `coding_hard` | Capability, N = 8; first baseline per (provider, model). |
