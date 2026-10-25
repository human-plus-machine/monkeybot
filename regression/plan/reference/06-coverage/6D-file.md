# 6.D File tools (`FILE`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/tools/workspace_service.py`, `core_tool_executor.py`, `patch.py`, `text_normalize.py`, `spill_inventory.py`, `core/path_safety.py`.

Existing: strong T1 (`test_core_tool_executor.py`, `test_patch.py`, `test_replace_fuzzy.py`, `test_gitignore.py`, `test_workspace_*.py`, `test_spill_*.py`, `test_path_safety.py`). T2 `tool_read`, `tool_write`, `tool_glob_grep`, `tool_replace_apply_patch` use the **recording** executor, so they only prove a tool name was routed. T4 `core_read`, `core_write` assert tool names and judge scores, not bytes.

Gaps: no T2 runs the real executor for escapes, stale reads, incomplete scans, patch failure, list truncation.

| ID | Case | Asserts |
|---|---|---|
| T1-FILE-01 | `tool_error_code_catalog` | Every `details.code` raised in `src/` (AST scan of `WorkspaceError(...)`, `PatchError(...)`, ledger errors) is in Appendix A's enum; an undocumented new code fails. |
| T1-FILE-02 | `envelope_shape_contract` | Every built-in tool error has `ok:false`, `error_kind ∈ {validation, policy, runtime, incomplete_scan, credential_egress_blocked}`, `message`, `hint`. Lists the known exceptions (`enable_mcp`, some `load_file`, `computer_*`) explicitly so a new exception fails. |
| T2-FILE-01 | `tool_read` (migrate, now real executor) | Content of a fixture file appears in the result. |
| T2-FILE-02 | `read_paging_no_gap` | Two pages via `next_offset` concatenate to the file. |
| T2-FILE-03 | `read_offset_past_eof` | `ok` with empty content (pins current behavior). |
| T2-FILE-04 | `read_escape_dotdot` | `../outside`: `invalid_path`; no outside bytes in any result. |
| T2-FILE-05 | `read_escape_symlink` | `link_out/x`: `path_escape`. |
| T2-FILE-06 | `read_absolute_needs_grant` | Absolute path outside workspace: `path_needs_grant`. |
| T2-FILE-07 | `read_huge_single_line` | Sliced with marker; no `next_offset`. |
| T2-FILE-08 | `tool_write` (migrate, real executor) | File created with exact bytes. |
| T2-FILE-09 | `write_new_without_read_ok` | New path writes without a prior read. |
| T2-FILE-10 | `overwrite_unread_rejected` | `read_before_write`; bytes unchanged. |
| T2-FILE-11 | `stale_read_rejected` | `steps: touch_file` between read and replace: `stale_read`; bytes unchanged; re-read then replace succeeds. |
| T2-FILE-12 | `skills_read_only` | Write to `skills/x/SKILL.md`: `skills_read_only`. |
| T2-FILE-13 | `write_outside_scope` | `WORKSPACE_WRITE_SCOPE_REL=src`, write `wide.txt`: `write_outside_scope`. |
| T2-FILE-14 | `write_payload_too_large` | Over `WORKSPACE_WRITE_MAX_BYTES`: `payload_too_large`. |
| T2-FILE-15 | `replace_error_codes` | One scenario per code: `not_found_replace`, `ambiguous_replace`, `no_change`, `empty_old_string`. |
| T2-FILE-16 | `replace_fuzzy_unicode` | Smart quotes in `old_string` match ASCII in file; `match_mode` reported. |
| T2-FILE-17 | `replace_apply_patch` (migrate, real executor) | Both edits land. |
| T2-FILE-18 | `patch_fail_closed` | One bad hunk: `hunk_not_found`; no file changed. |
| T2-FILE-19 | `patch_rollback` | Injected mid-apply failure: `rollback_failed`; `details` lists dirty paths. |
| T2-FILE-20 | `patch_add_existing` / `patch_move_dest_exists` | `already_exists`. |
| T2-FILE-21 | `patch_update_requires_read` | Update/Delete of unread file: `read_before_write`. |
| T2-FILE-22 | `glob_skips_noise` | `**/*` omits `node_modules`, `.venv`, `build`; `node_modules/**` includes it. |
| T2-FILE-23 | `glob_cap_incomplete` | Over `WORKSPACE_GLOB_MAX_PATHS`: `ok:false`, `incomplete_scan`, never an empty success. |
| T2-FILE-24 | `grep_gitignore_default` | Default misses `secret.txt`; `respect_gitignore:false` finds it; nested `pkg/.gitignore` scoped. |
| T2-FILE-25 | `grep_incomplete_scan` | Oversized file in scope: `incomplete_scan`. |
| T2-FILE-26 | `grep_pagination` | `max_matches` + `offset` page through all matches; `total_match_count` stable. |
| T2-FILE-27 | `grep_rg_vs_python_parity` | Same results with rg present and absent for an rg-safe pattern; lookaround pattern uses Python. |
| T2-FILE-28 | `grep_invalid_regex` | `invalid_regex`. |
| T2-FILE-29 | `list_dir_truncate` | 301 entries: `truncated:true`, `count:301`. |
| T2-FILE-30 | `virtual_mounts` | `skills/` and `artifacts/` resolve to their roots; no fallback between roots. |
| T2-FILE-31 | `spill_large_tool_output` | Over `spill_threshold`: spill path returned; follow-up `read_file` of the spill path works past the default line cap. |
| T2-FILE-32 | `spill_session_id_sanitized` | Session id with `..` cannot write outside the spill tree. |
| T2-FILE-33 | `tool_glob_grep` (migrate) | As today: glob and grep both return the fixture's matches. |
| T4-FILE-01 | `core_read` (migrate) | Plus: answer quotes a fixture line (code grader). |
| T4-FILE-02 | `core_read_error` (migrate; add to smoke) | Model recovers from a bad path; `max_tool_errors: 0` after forgiveness. |
| T4-FILE-03 | `core_write` (migrate) | Plus: `workspace_file_contains`. |
| T4-FILE-04 | `grep_absence_honesty` | After `incomplete_scan`, the answer does not claim absence (`response_not_contains` + judge). |
