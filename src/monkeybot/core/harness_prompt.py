"""Runtime-owned harness prompt fragment.

The agent loop appends :func:`harness_fixed_context` after the operator-authored
base prompt (AGENT.md) so tool/MCP protocol text lives in code, not in the bot file.
"""

HARNESS_TOOL_CALL_PROTOCOL = """
### Tool-call protocol (strict)
- Invoke tools only through the provider's native function-call channel. Never emit tool invocations as JSON or pseudo-XML inside your assistant text; any such text is treated as a normal message and no tool will run.
- After tool results are returned to you, your next response MUST be natural-language text that addresses the user's request using those results. Do not return another empty turn.
- If you have nothing more to do, give a short final answer; do not stay silent."""


_HARNESS_BODY = """## MonkeyBot harness (fixed)

This block is injected by the host every turn. Prefer the **active tool list** the model receives over any stale summary here.

### Core built-in tools (when present in the active tool list)
- `read_file` / `write_file` — paths are **workspace-relative** (repository root the process uses).
- `search_memory` — keyword search under the configured memory directory; prefer this over shell commands for any memory lookup.
- `list_skills` — lists installed skills; read each skill's `SKILL.md` under the skills root for procedure.
- `run_command` — allowlisted shell with optional `timeout` (seconds). Shell starts in **workspace root**; use the paths listed under Runtime paths below — do NOT guess directory names. `cd` is a shell builtin and cannot be used as a bare command; use `bash -c "cd <dir> && <cmd>"` instead.
- `add_mcp_server` / `remove_mcp_server` — register or drop MCP stdio servers; new tools appear on later turns.
{web_search_line}{task_line}
### Built-in tool errors (recovery)
- Failed built-in tools often return **JSON** with `ok: false`, `error_kind` (`policy` | `validation` | `runtime`), `message`, and `hint`.
- If `error_kind` is **policy** (e.g. `run_command` blocked), **do not** retry the identical call; change the command or path per `hint` and the lists in `details`, then retry **once** after a single fix.
- If `error_kind` is **validation**, fix the argument shape (see `details.example`), then retry once.

### Runtime paths
- workspace root: `{workspace_root}`
- memory directory: `{memory_path}` — always use `search_memory` to query; only use this path directly in `run_command` for low-level inspection.

### MCP tools
- Names look like `server__tool` (double underscore).

### Skills
- Use `list_skills` then `read_file` on the skill's `SKILL.md` before running commands a skill documents."""


_TASK_LINE = (
    "- `task` — subprocess subagent with the same workspace, memory, and MCP configuration; "
    "returns JSON (summary, errors, usage). Nested `task` is disabled inside a subagent.\n"
)

_WEB_SEARCH_LINE = (
    "- `web_search` — search the web for current information; "
    "returns titles, URLs, and text snippets.\n"
)


def harness_fixed_context(
    *,
    include_task_tool: bool,
    include_web_search: bool = False,
    workspace_root: str = "(not set)",
    memory_path: str = "(not set)",
) -> str:
    """Runtime-owned description of core tools, paths, MCP naming, and strict tool-call rules.

    ``workspace_root`` and ``memory_path`` are resolved absolute paths injected once at
    context-build time so the model always uses correct paths in shell commands.
    ``include_web_search`` should be True when a web search backend is active.
    """
    body = _HARNESS_BODY.format(
        web_search_line=_WEB_SEARCH_LINE if include_web_search else "",
        task_line=_TASK_LINE if include_task_tool else "",
        workspace_root=workspace_root,
        memory_path=memory_path,
    )
    return body.rstrip() + HARNESS_TOOL_CALL_PROTOCOL
