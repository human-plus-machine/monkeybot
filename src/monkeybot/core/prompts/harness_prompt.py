"""Runtime-owned harness prompt fragment.

The agent loop appends :func:`harness_fixed_context` after the operator-authored
base prompt (AGENT.md) so tool/MCP protocol text lives in code, not in the bot file.
"""

import os
from collections.abc import Sequence

HARNESS_TOOL_CALL_PROTOCOL = """
### Tool-call protocol (strict)
- Invoke tools only through the provider's native function-call channel. Never emit tool invocations as JSON or pseudo-XML inside your assistant text; any such text is treated as a normal message and no tool will run.
- After tool results are returned to you, your next response MUST be natural-language text that addresses the user's request using those results. Do not return another empty turn.
- If you have nothing more to do, give a short final answer; do not stay silent.
- **Evidence rule:** Never produce a substantive answer about content you were supposed to fetch but could not. If every tool path to that content failed or errored, tell the user what blocked you and what they need to supply — do not synthesize, guess, or hallucinate the missing content.
- **Fulfillment rule:** When the user asks for a file or code change and the relevant tools are available, use them. Do not answer with only pasted code and manual save instructions."""


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

When workspace tools (`read_file`, `write_file`, `run_command`, …) appear in that list, you **have a writable workspace** — not a read-only chat window. Fulfill file and code requests with tools; do not paste full artifacts and instruct the user to save manually unless they explicitly asked to see code in chat.

### Core built-in tools (when present in the active tool list)
- `read_file` / `write_file` / `replace_in_file` / `glob` — paths are **workspace-relative** under the workspace root below. **`glob`** lists matching files; prefer it over `run_command` + `ls` for discovery. Do not substitute a code block in chat for a file deliverable.
- `search_memory` — keyword search under the configured memory directory; prefer this over shell commands for any memory lookup.
- `list_skills` — lists installed skills; read each skill's `SKILL.md` under the skills root for procedure.
- `run_command` — allowlisted shell with optional `timeout` (seconds). {run_command_exec_note} Shell starts in **workspace root**; use the paths listed under Runtime paths below — do NOT guess directory names. `cd` is a shell builtin and cannot be used as a bare command; use `bash -c "cd <dir> && <cmd>"` instead. Pass **`argv` as a list** with the binary first (e.g. `{{"argv": ["ls", "."]}}`); do not pass `{{"command": "ls -R", "args": []}}` — that treats `ls -R` as the binary name.
- `add_mcp_server` / `remove_mcp_server` — register or drop MCP stdio servers; new tools appear on later turns.
{web_search_line}{task_line}
### Workspace deliverables
- **New file or full rewrite** → `write_file`.
- **Targeted change to an existing file** → `read_file` then `replace_in_file` (`old_string` must match exactly once).
- Tell the user the workspace-relative path when done.
- **Do not claim** you lack filesystem access, cannot touch the user's machine, or are limited to "chat-only" output when workspace file tools are in the active tool list.
- Chat text is for answers and brief excerpts — not a stand-in for a file the user asked you to produce.

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
    "pass `subagent_type` to select a named persona (see Subagent personas below). "
    "Returns JSON (summary, errors, usage). Nested `task` is disabled inside a subagent.\n"
)

_WEB_SEARCH_LINE = (
    "- `web_search` — search the web for current information; "
    "returns titles, URLs, and text snippets.\n"
)


# Always-on terse-emission guidance (Levers 1-2 of the honey writing style).
# Trimmed to rules + safety carve-outs; no examples (volume is cost). Lives in
# the stable harness prefix so it caches, and never overrides the evidence or
# no-repeat rules above. Opt-in via ``emission_style`` (env MONKEYBOT_EMISSION_STYLE).
_EMISSION_STYLE_BLOCK = """
### Emission style (terse)

Volume is cost. Default to terse; add detail only when brevity would drop correctness.

- **Code:** write the minimum that needs to exist. Prefer stdlib and installed deps over new code; edit over add; one line over a block. No speculative params, "might need it later" branches, or single-caller abstractions.
- **Prose:** answer first. Drop wind-up, hedging, and restating the request. Use fragments/lists over paragraphs when they carry the same info. Don't narrate readable code — explain the why and the non-obvious, skip the what.
- **Keep exact, never compress:** code blocks verbatim and runnable; identifiers, paths, commands, versions, and error messages quoted exactly.

Never cut (brevity must not break correctness):
- Input validation at trust boundaries, error handling that prevents data loss, and security/auth checks.
- The blocker report when every tool path failed — state what failed and what's needed; do not synthesize missing content (see the evidence rule below).
- Anything the user explicitly asked for.

When editing code, keep function bodies — signatures alone are not enough to edit correctly.
"""

# Lever 3: dense agent-to-agent handoffs. Only meaningful when the `task` tool
# is active, so the caller gates this on ``include_task_tool``.
_EMISSION_AGENT_TO_AGENT_BLOCK = """
### Subagent handoffs (dense)

When the reader is a subagent or orchestrator (`task` results, not user-facing answers), emit the densest format the receiver parses losslessly:
- Minified JSON, never pretty-printed.
- Address records by stable key, not position ("the finding with id X", not "the 37th").
- Aggregate in code; pass counts, not rows for the receiver to count. Number rows only if positional access is unavoidable.
- For uniform record arrays, prefer columnar: keys once, then value rows.
"""


def emission_style_terse_from_env() -> bool:
    """True when ``MONKEYBOT_EMISSION_STYLE`` opts into the terse emission block."""
    raw = os.environ.get("MONKEYBOT_EMISSION_STYLE", "").strip().lower()
    return raw in {"terse", "true", "1", "on", "yes"}


def _emission_section(*, emission_style: bool, include_task_tool: bool) -> str:
    """Stable-prefix emission guidance; empty unless terse style is opted in."""
    if not emission_style:
        return ""
    section = _EMISSION_STYLE_BLOCK
    if include_task_tool:
        section += _EMISSION_AGENT_TO_AGENT_BLOCK
    return section


def _subagent_personas_block(personas: Sequence[tuple[str, str]]) -> str:
    if not personas:
        return ""
    lines = ["\n### Subagent personas (`task` tool)", ""]
    for name, description in personas:
        lines.append(f"- `{name}` — {description}")
    lines.append(
        "Pass `subagent_type` on `task` to select a persona. Omit for the default subagent AGENT.md."
    )
    return "\n".join(lines) + "\n"


def harness_fixed_context(
    *,
    include_task_tool: bool,
    include_web_search: bool = False,
    workspace_root: str = "(not set)",
    memory_storage_uri: str = "(not set)",
    run_command_opensandbox: bool = False,
    subagent_personas: Sequence[tuple[str, str]] | None = None,
    emission_style: bool = False,
) -> str:
    """Runtime-owned description of core tools, paths, MCP naming, and strict tool-call rules.

    ``workspace_root`` and ``memory_storage_uri`` are injected once at
    context-build time so the model always uses correct paths in shell commands.
    ``include_web_search`` should be True when a web search backend is active.
    ``run_command_opensandbox`` should match whether ``run_command`` is routed through
    OpenSandbox (same signal as ``SandboxConfig.from_env().enabled``).
    ``subagent_personas`` lists configured named subagent types for the parent orchestrator.
    ``emission_style`` opts in the terse emission-guidance block (env
    ``MONKEYBOT_EMISSION_STYLE``); the dense agent-to-agent sub-block is also gated
    on ``include_task_tool`` so it only appears when the ``task`` tool is active.
    """
    exec_note = _RUN_COMMAND_EXEC_NOTE_SANDBOX if run_command_opensandbox else _RUN_COMMAND_EXEC_NOTE_HOST
    body = _HARNESS_BODY.format(
        run_command_exec_note=exec_note,
        web_search_line=_WEB_SEARCH_LINE if include_web_search else "",
        task_line=_TASK_LINE if include_task_tool else "",
        workspace_root=workspace_root,
        memory_storage_uri=memory_storage_uri,
    )
    personas_block = _subagent_personas_block(subagent_personas or ())
    emission_block = _emission_section(
        emission_style=emission_style,
        include_task_tool=include_task_tool,
    )
    return body.rstrip() + personas_block + emission_block + HARNESS_TOOL_CALL_PROTOCOL
