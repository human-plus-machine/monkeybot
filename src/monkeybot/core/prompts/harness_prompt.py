"""Runtime-owned harness prompt fragment.

The agent loop appends :func:`harness_fixed_context` after the operator-authored
base prompt (AGENT.md) so tool/MCP protocol text lives in code, not in the bot file.

Tool names, parameters, and when-to-use guidance live in the JSON ``tools`` payload
(``ToolDef.to_model_schema()``). This fragment is protocol + paths only — not a
second catalog of the same tools.
"""

from collections.abc import Sequence

from monkeybot.core.config.snapshot import current_env

HARNESS_TOOL_CALL_PROTOCOL = """
### Tool-call protocol (strict)
- Invoke tools only through the provider's native function-call channel. Never emit tool invocations as JSON or pseudo-XML inside your assistant text; any such text is treated as a normal message and no tool will run.
- After tool results are returned to you, your next response MUST be natural-language text that addresses the user's request using those results. Do not return another empty turn.
- If you have nothing more to do, give a short final answer; do not stay silent.
- **Evidence rule:** Never produce a substantive answer about content you were supposed to fetch but could not. If every tool path to that content failed or errored, tell the user what blocked you and what they need to supply — do not synthesize, guess, or hallucinate the missing content.
- **Path rule:** Never emit a workspace file path (including `Evidence:` lines) you have not confirmed via `read_file` / `glob` this session — a `search` hit alone is a lead, not confirmation. If the path is unknown, say `unknown` — do not guess filenames.
- **Fulfillment rule:** When the user asks for a file or code change and the relevant tools are available, use them. Do not answer with only pasted code and manual save instructions."""


_RUN_COMMAND_EXEC_NOTE_HOST = (
    "**Execution:** on the **gateway host** under the binary/path allowlist only "
    "(no OpenSandbox / no extra VM)."
)
_RUN_COMMAND_EXEC_NOTE_SANDBOX = (
    "**Execution:** via **OpenSandbox** (Docker-backed session container). "
    "The workspace root is bind-mounted at the **same absolute path**; still subject to the allowlist."
)

_HARNESS_BODY = """## monkeybot harness (fixed)

This block is injected by the host every turn. Prefer the **active JSON tool list** for names, parameters, and when-to-use guidance.

### Built-in tool errors (recovery)
- A tool **failed** whenever its response contains `ok: false` (or `is_error`). Read the result; a non-empty response is not success.
- Failed built-in tools often return JSON with `ok: false`, `error_kind` (`policy` | `validation` | `runtime`), `message`, and `hint`.
- **policy:** do not retry the identical call; change command or path per `hint`, then retry once.
- **validation:** fix the argument shape (see `details.example`), then retry once.
- **runtime:** do not re-run the identical call. Fix the cause if you can, or stop and tell the user what is missing.
- **No-repeat rule:** never issue a tool call with the same name and same arguments that already failed this turn. If you cannot change anything, stop and report the blocker in plain text.
- **Spill / partial artifacts:** on timeout, truncation, interrupt, or any result that mentions a spill path / `details.partial_output_path` / `partial_paths`, **`read_file` that path** (or the spill inventory under `.monkeybot/`) **before** changing args or re-issuing the tool.

### Runtime paths
- workspace root (cwd): `{workspace_root}` — file and shell tools start here; `run_command` may set a workspace-relative `cwd`.
- `run_command`: {run_command_exec_note}
- runtime (inside workspace): `.monkeybot/` — spill, knowledge index, transcripts. Not memory.
{memory_paths_line}- workspace `data/` (if present) is ordinary project files — **not** the memory store.
- **Long multi-item tasks:** when a task has more than ~10 enumerable items (question lists, checklists), write incremental results to a workspace file early and update it as you go — context may be compacted mid-task.

### MCP
- Names look like `server__tool` (double underscore). Call `enable_mcp` before using a server's tools when they are not yet in the active list; resource/prompt tools appear only after `enable_mcp`.
{catalog_mcp_line}- MCP errors are plain text (not structured JSON). HTTP 4xx/5xx, "not found", "unauthorized", "forbidden", or similar means the tool **did not return usable data** — state what failed; do not fabricate content.
- Call `enable_loops` before scheduled-loop tools appear.

### Skills
- Installed skill names are listed under `## Skills` in this prompt. When a task matches one, use `list_skills` to get the skills root, then `read_file` that skill's `SKILL.md` before following it."""


def _memory_paths_line(*, memory_on: bool, memory_storage_uri: str) -> str:
    if memory_on:
        return (
            f"- memory storage: `{memory_storage_uri}` — MemPalace root (verbatim conversation "
            "drawers). **Outside** the workspace root. Prefer `mempalace search` via "
            "`run_command` for past-session recall. Do not `read_file` palace paths.\n"
        )
    return "- memory storage: disabled — do not call `mempalace search` or read palace paths.\n"


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
    raw = current_env("MONKEYBOT_EMISSION_STYLE", "").strip().lower()
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
    workspace_root: str = "(not set)",
    memory_storage_uri: str = "(not set)",
    run_command_opensandbox: bool = False,
    subagent_personas: Sequence[tuple[str, str]] | None = None,
    emission_style: bool = False,
    memory_on: bool = True,
    catalog_mcp_servers: Sequence[str] | None = None,
) -> str:
    """Runtime-owned protocol, paths, MCP naming, and strict tool-call rules.

    Tool catalogs belong in the JSON ``tools`` payload, not this markdown.
    ``workspace_root`` and ``memory_storage_uri`` are injected once at
    context-build time so the model always uses correct paths in shell commands.
    ``run_command_opensandbox`` should match whether ``run_command`` is routed through
    OpenSandbox (same signal as ``SandboxConfig.from_env().enabled``).
    ``subagent_personas`` lists configured named subagent types for the parent orchestrator.
    ``emission_style`` opts in the terse emission-guidance block (env
    ``MONKEYBOT_EMISSION_STYLE``); the dense agent-to-agent sub-block is also gated
    on ``include_task_tool`` so it only appears when the ``task`` tool is active.
    ``memory_on`` selects the memory-storage path line (URI vs disabled).
    ``catalog_mcp_servers`` lists mcp.json servers available via ``enable_mcp`` but not
    connected until the model activates them.
    """
    exec_note = (
        _RUN_COMMAND_EXEC_NOTE_SANDBOX if run_command_opensandbox else _RUN_COMMAND_EXEC_NOTE_HOST
    )
    catalog = [n.strip() for n in (catalog_mcp_servers or ()) if n and str(n).strip()]
    if catalog:
        names = ", ".join(f"`{n}`" for n in catalog)
        catalog_mcp_line = f"- Configured MCP servers (call `enable_mcp` before use): {names}.\n"
    else:
        catalog_mcp_line = ""
    body = _HARNESS_BODY.format(
        run_command_exec_note=exec_note,
        catalog_mcp_line=catalog_mcp_line,
        memory_paths_line=_memory_paths_line(
            memory_on=memory_on,
            memory_storage_uri=memory_storage_uri,
        ),
        workspace_root=workspace_root,
    )
    personas_block = _subagent_personas_block(subagent_personas or ())
    emission_block = _emission_section(
        emission_style=emission_style,
        include_task_tool=include_task_tool,
    )
    return body.rstrip() + personas_block + emission_block + HARNESS_TOOL_CALL_PROTOCOL
