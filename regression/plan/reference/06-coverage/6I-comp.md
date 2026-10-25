# 6.I Computer-use tools (`COMP`, `darwin_only`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `computer/tools.py`, `safety.py`, `permissions.py`, `approvals.py`.

Existing: T1 `tests/computer/*` (policy suite), `test_subagent_exclusion.py` (AST: no import).

| ID | Case | Asserts |
|---|---|---|
| T1-COMP-01 | `not_advertised_off_darwin` | `should_enable_computer_tools` false on non-darwin regardless of config. |
| T2-COMP-01 | `open_exec_suffix_denied` | `computer_open x.sh`: `error_kind: policy`. |
| T2-COMP-02 | `denied_dirs_and_globs` | `~/.ssh`, `~/.aws`, `*.pem`, `.env`: denied for list/find/move/trash. |
| T2-COMP-03 | `denied_apps_and_schemes` | Terminal / Cursor apps; `file:` URL: denied. |
| T2-COMP-04 | `clipboard_cap` | 20 001-char write: rejected. |
| T2-COMP-05 | `move_trash_not_always_scoped` | "Always allow" on `computer_trash` does not persist. |
| T2-COMP-06 | `trash_huge_tree_refused` | `check_trashable` refuses. |
