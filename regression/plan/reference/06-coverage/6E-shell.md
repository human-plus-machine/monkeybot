# 6.E Shell, background jobs, jail, sandbox (`SHELL`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `core/tools/terminal.py`, `inspector.py` (`parse_run_command`), `fs_isolation.py`, `sandbox_executor.py`, `core/subprocess_groups.py`.

Existing: T1 `test_terminal.py`, `test_fs_isolation.py`, `test_sandbox_executor.py` (mocked SDK), `test_background_jobs.py`, `test_run_command_parse.py`. T2 `tool_run_command.yaml` recording only. T4 `core_run_command`; `core_run_command_sandbox` local-only.

| ID | Case | Asserts |
|---|---|---|
| T2-SHELL-01 | `run_command` (migrate, real executor) | `echo` output and `exit_code: 0`. |
| T2-SHELL-02 | `argv_forms` | `argv` list, JSON string, `command`+`args`, `shell` all parse and run. |
| T2-SHELL-03 | `allowlist_deny` | `curl`: `error_kind: policy`; not executed (no side-effect file). |
| T2-SHELL-04 | `shell_wrapper_bypass` | `bash -c 'pip install x'`: denied by deny patterns before execute. |
| T2-SHELL-05 | `unscreenable_script_fails_closed` | Deeply nested `bash -c` beyond the walk depth: denied. |
| T2-SHELL-06 | `home_escape` | `cat ~/.ssh/id_rsa` and `bash -c 'cat $HOME/.ssh/x'`: no bytes read (path policy or jail). |
| T2-SHELL-07 | `cwd_escape` | `cwd: ../`: `invalid_cwd`. |
| T2-SHELL-08 | `nonzero_exit` | `python -c 'raise SystemExit(3)'`: `ok:false`, `exit_code:3`, streams present. |
| T2-SHELL-09 | `timeout_partial_output` | Timeout: error with `partial_output_path`; process group gone. |
| T2-SHELL-10 | `large_stdout_spill_keeps_exit` | Huge stdout: `output_path` set; real `exit_code` preserved. |
| T2-SHELL-11 | `bg_start_await_cursor` | `background:true` then two `await_command`s: second page `has_more:false`, no duplicated prefix. |
| T2-SHELL-12 | `bg_job_cap` | Job over `max_background_jobs`: cap error. |
| T2-SHELL-13 | `bg_kill_command` | `kill_command` stops the job; `await_command` reports killed. |
| T2-SHELL-14 | `bg_killed_at_turn_end` | Job still running at end: killed by `aclose`; turn guard nudged first (LOOP-16). |
| T2-SHELL-15 | `mempalace_search_only` | `mempalace edit|update|forget|repair`: `SecurityError`; `search` allowed when memory on. |
| T2-SHELL-16 | `mempalace_refused_memory_off` | Memory off: policy error, no spawn. |
| T2-SHELL-17 | `jail_fail_open_pinned` (`slow`, host-dependent) | When the jail cannot start, caches are not rewritten and a warning is logged. Skip on hosts without seatbelt/namespaces. |
| T3-SHELL-01 | `replay_live_pytest_run` | Recorded live `coding/fix_failing_test` cassette replays against `calc_repo` and ends with pytest green. |
| T4-SHELL-01 | `core_run_command` (migrate) | Plus: a file created by the command exists. |
| T4-SHELL-02 | `core_run_command_sandbox` (migrate, `local-only`) | Same through OpenSandbox. |
