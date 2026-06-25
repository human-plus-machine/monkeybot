"""Runtime-owned harness prompt fragment.

The agent loop appends :func:`harness_fixed_context` after the operator-authored
base prompt (AGENT.md) so tool/MCP protocol text lives in code, not in the bot file.
"""

HARNESS_TOOL_CALL_PROTOCOL = """
### Tool-call protocol (strict)
- Invoke tools only through the provider's native function-call channel. Never emit tool invocations as JSON or pseudo-XML inside your assistant text; any such text is treated as a normal message and no tool will run.
- After tool results are returned to you, your next response MUST be natural-language text that addresses the user's request using those results. Do not return another empty turn.
- If you have nothing more to do, give a short final answer; do not stay silent.
- **Evidence rule:** Never produce a substantive answer about content you were supposed to fetch but could not. If every tool path to that content failed or errored, tell the user what blocked you and what they need to supply — do not synthesize, guess, or hallucinate the missing content."""


_RUN_COMMAND_EXEC_NOTE_HOST = (
    "**Execution:** on the **gateway host** under the binary/path allowlist only "
    "(no OpenSandbox / no extra VM)."
)
_RUN_COMMAND_EXEC_NOTE_SANDBOX = (
    "**Execution:** via **OpenSandbox** (Docker-backed session container). "
    "The workspace root is bind-mounted at the **same absolute path**; still subject to the allowlist."
)

_HARNESS_BODY = """## MonkeyBot harness (fixed)

This block is injected by the host every turn. Prefer the **active tool list** the model receives over any stale summary here.

### Core built-in tools (when present in the active tool list)
- `read_file` / `write_file` — paths are **workspace-relative** (repository root the process uses).
- `search_memory` — keyword search under the configured memory directory; prefer this over shell commands for any memory lookup.
- `list_skills` — lists installed skills; read each skill's `SKILL.md` under the skills root for procedure.
- `run_command` — allowlisted shell with optional `timeout` (seconds). {run_command_exec_note} Shell starts in **workspace root**; use the paths listed under Runtime paths below — do NOT guess directory names. `cd` is a shell builtin and cannot be used as a bare command; use `bash -c "cd <dir> && <cmd>"` instead.
- `add_mcp_server` / `remove_mcp_server` — register or drop MCP stdio servers; new tools appear on later turns.
{web_search_line}{task_line}
### Built-in tool errors (recovery)
- A tool **failed** whenever its response contains `ok: false` (or `is_error`), even if the call itself "succeeded" (e.g. `run_command` returns `exit_code != 0` with an `ok:false` JSON body in `stdout`). Read the result; do not treat a non-empty response as success.
- Failed built-in tools often return **JSON** with `ok: false`, `error_kind` (`policy` | `validation` | `runtime`), `message`, and `hint`.
- If `error_kind` is **policy** (e.g. `run_command` blocked), **do not** retry the identical call; change the command or path per `hint` and the lists in `details`, then retry **once** after a single fix.
- If `error_kind` is **validation**, fix the argument shape (see `details.example`), then retry once.
- If `error_kind` is **runtime** (the command/tool ran but failed — e.g. missing config, missing env var, bad exit code, script error), **do not** re-run the identical call. The same inputs will produce the same failure. Either fix the underlying cause if you can act on it, or stop and tell the user exactly what is missing (e.g. an env var, project id, or credential) and what they must set.
- **No-repeat rule (applies to every tool):** never issue a tool call with the same name and same arguments that already failed this turn. A retry is only allowed after you have changed the command, arguments, or path in response to the error. If you cannot change anything, stop retrying and report the blocker in plain text.

### Runtime paths
- workspace root: `{workspace_root}`
- memory storage: `{memory_storage_uri}` — always use `search_memory` to query; only use this URI/path directly in `run_command` for low-level inspection.

### MCP tools
- Names look like `server__tool` (double underscore).
- MCP tool errors are returned as plain error text (not structured JSON). Any response containing an HTTP error code (4xx / 5xx), "not found", "unauthorized", "forbidden", "permission denied", or similar access/availability signals means the tool **did not return usable data**.
- When an MCP tool fails: state what failed in one sentence, then stop — do **not** fabricate, infer, or summarize content that the tool was supposed to fetch. If a fallback tool is available and meaningfully different, try it once; otherwise tell the user what is needed to proceed (e.g. correct credentials, a public URL, pasting the content directly).

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
    memory_storage_uri: str = "(not set)",
    run_command_opensandbox: bool = False,
) -> str:
    """Runtime-owned description of core tools, paths, MCP naming, and strict tool-call rules.

    ``workspace_root`` and ``memory_storage_uri`` are injected once at
    context-build time so the model always uses correct paths in shell commands.
    ``include_web_search`` should be True when a web search backend is active.
    ``run_command_opensandbox`` should match whether ``run_command`` is routed through
    OpenSandbox (same signal as ``SandboxConfig.from_env().enabled``).
    """
    exec_note = _RUN_COMMAND_EXEC_NOTE_SANDBOX if run_command_opensandbox else _RUN_COMMAND_EXEC_NOTE_HOST
    body = _HARNESS_BODY.format(
        run_command_exec_note=exec_note,
        web_search_line=_WEB_SEARCH_LINE if include_web_search else "",
        task_line=_TASK_LINE if include_task_tool else "",
        workspace_root=workspace_root,
        memory_storage_uri=memory_storage_uri,
    )
    return body.rstrip() + HARNESS_TOOL_CALL_PROTOCOL
