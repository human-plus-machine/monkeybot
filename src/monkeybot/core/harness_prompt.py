"""Runtime-owned harness prompt fragment.

The agent loop appends :func:`harness_fixed_context` after AGENT.md so per-bot
files stay persona-only and tool/MCP wiring lives here.
"""

HARNESS_TOOL_CALL_PROTOCOL = """
### Tool-call protocol (strict)
- To invoke a tool, use the provider's native function/tool-call channel only. Do NOT write a JSON object such as `{"tool_calls": [...]}` in your assistant text — history rows that look like that are stored placeholders, not a wire format you should imitate.
- After tool results are returned to you, your next response MUST be natural-language text that addresses the user's request using those results. Do not return another empty turn.
- If you have nothing more to do, give a short final answer; do not stay silent."""


_HARNESS_BODY = """## MonkeyBot harness (fixed)

This block is injected by the host every turn. Prefer the **active tool list** the model receives over any stale summary here.

### Core built-in tools (when present in the active tool list)
- `read_file` / `write_file` — paths are **workspace-relative** (repository root the process uses).
- `search_memory` — keyword search under the configured memory directory; open files when you need exact text.
- `list_skills` — lists installed skills; read each skill's `SKILL.md` under the skills root for procedure.
- `run_command` — allowlisted shell with optional `timeout` (seconds).
- `add_mcp_server` / `remove_mcp_server` — register or drop MCP stdio servers; new tools appear on later turns.
{task_line}
### MCP tools
- Names look like `server__tool` (double underscore).

### Skills
- Use `list_skills` then `read_file` on the skill's `SKILL.md` before running commands a skill documents."""


_TASK_LINE = (
    "- `task` — subprocess subagent with the same workspace, memory, and MCP configuration; "
    "returns JSON (summary, errors, usage). Nested `task` is disabled inside a subagent.\n"
)


def harness_fixed_context(*, include_task_tool: bool) -> str:
    """Runtime-owned description of core tools, MCP naming, and strict tool-call rules.

    ``include_task_tool`` should match whether ``task`` is in the active tool list for this turn.
    """
    body = _HARNESS_BODY.format(task_line=_TASK_LINE if include_task_tool else "")
    return body.rstrip() + HARNESS_TOOL_CALL_PROTOCOL
