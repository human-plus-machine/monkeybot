# 6.F Policy: inspectors, grants, permissions (`POL`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/tools/inspector.py` (`CommandTierInspector`, `RulesInspector`, `PathGrantInspector`, `PermissionInspector`, `LoopStartInspector`), `grant_store.py`, `computer/permissions.py`.

Existing: T1 `test_inspector.py`, `test_permission.py`, `test_path_grant_inspector.py`, `test_grant_store.py`, `test_workspace_extra_read_roots.py`. No T2.

| ID | Case | Asserts |
|---|---|---|
| T1-POL-01 | `policy_decision_table` | Table-driven: (tool, args, policy fixture) → `allow|deny|confirm` + `grant_kind` for ~40 rows across all inspectors. One place to see the whole policy. |
| T1-POL-02 | `empty_allowed_commands_raises` | Loading an allowlist with `allowed_commands: []` raises. |
| T1-POL-03 | `corrupt_grants_load_empty` | Bad JSON in `grants.json` loads as empty, logs once. |
| T2-POL-01 | `unknown_binary_confirm_approve` | Unlisted binary: `ToolConfirmationRequest`; approve executes. |
| T2-POL-02 | `unknown_binary_confirm_deny` | Deny: not executed. |
| T2-POL-03 | `default_deny_patterns` | Omitted `deny_patterns`: `pip install`, `uv add`, `npm i`, `brew install` denied. |
| T2-POL-04 | `path_grant_read` | Absolute read without grant: `path_needs_grant`; with `turn_path_grants`: reads in place. |
| T2-POL-05 | `granted_folder_credential_denied` | `.env` in a granted folder: `credential_denied`; omitted from `grep` results. |
| T2-POL-06 | `symlink_in_grant_does_not_widen` | Symlink inside grant pointing outside: denied. |
| T2-POL-07 | `command_grant_from_store` | Binary listed in `grants.json` runs without confirm. |
| T2-POL-08 | `grant_revocation_clears` | Removing a path grant between turns removes read access (replace, not merge). |
| T2-POL-09 | `permissions_last_match_wins` | Two overlapping rules: last wins. |
| T2-POL-10 | `permissions_missing_file_fail_open` | Pins current behavior (documented risk). |
| T2-POL-11 | `start_loop_always_confirms` | `start_loop` always yields a confirmation, even with an "always" approval. |
