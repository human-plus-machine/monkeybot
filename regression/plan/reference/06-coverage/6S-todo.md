# 6.S Todo list (`TODO`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `todo_list/*` (`store.py`, `tool.py`, reminder).

Existing: T1 `tests/todo_list/test_todo_list.py`. T2 `tool_todo_list`, `tool_todo_list_steps` (recording).

| ID | Case | Asserts |
|---|---|---|
| T2-TODO-01 | `todo_list` / `todo_list_steps` (migrate, real tool) | List state after the turn matches the scripted calls. |
| T2-TODO-02 | `single_in_progress` | Setting a second item `in_progress` demotes or rejects per current rule. |
| T2-TODO-03 | `caps` | 51st item / 501-char item rejected. |
| T2-TODO-04 | `ids_stable` | `tN` ids survive clear/remove/hydrate. |
| T2-TODO-05 | `reminder_once` | Stale-list reminder fires once per store instance until the list changes. |
| T2-TODO-06 | `mirror_hydrate` | `mirror_to_disk`: `todos.json` written; new store hydrates same ids and one `in_progress`. |
| T4-TODO-01 | `multi_turn/task_tracking` (migrate) | Plus: `todos.json` matches the tool trace. |
| T5-TODO-01 | trials of T4-TODO-01 | Reliability on "exactly one in_progress at any time". |
