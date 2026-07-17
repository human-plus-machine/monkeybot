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

This block is injected by the host every turn. Prefer the **active tool list** the model receives over any stale summary here.

When workspace tools (`read_file`, `write_file`, `run_command`, …) appear in that list, you **have a writable workspace** — not a read-only chat window. Fulfill file and code requests with tools; do not paste full artifacts and instruct the user to save manually unless they explicitly asked to see code in chat.

### Core built-in tools (when present in the active tool list)
- `read_file` / `write_file` / `replace_in_file` / `glob` / `grep` / `apply_patch` — paths are **workspace-relative** under the workspace root below. **`glob`** lists matching files (prefer over `run_command` + `ls`). **`grep`** searches file contents with a regex (prefer over `run_command` + `grep`). **`apply_patch`** applies a multi-file Codex-style patch (Add / Update / Delete / Move) fail-closed. Do not substitute a code block in chat for a file deliverable.
- `search_memory` — keyword search under the configured memory directory (delegates to `search` when the knowledge layer is on).
- `search` — **local search index over the workspace** (and optional notes): FTS + link graph + optional embeddings. This is *not* notes-only and *not* a substitute for `read_file` — it finds which files to open. Prefer `search` for unfamiliar codebases, conceptual / cross-file / paraphrased questions; prefer `grep` for exact identifiers, filenames, or stack traces. (`recall` is a legacy alias.)

### Knowledge retrieval (`search`) — how to use it well
- **Default:** when exploring or answering questions about code you have not already located, **call `search` before** broad `glob` / wandering `read_file`. Do **not** skip it because "the answer is in source" — the index *is* that source.
- **When:** "where does X live?", "how does Y work?", multi-question repo Q&A, paraphrases without exact symbols, or when you lack a precise path/`grep` needle.
- **Locate-a-file / asset questions:** for "where is the logo / hero image / font file?" prefer `glob` on the plausible directory (e.g. `**/public/**`, `**/assets/**`) over `search`.
- **Query shape:** one short, focused query per concept using distinctive nouns (mechanism, module role, behavior) — not the user's full multi-question dump pasted into ten parallel calls.
- **Batching:** if answering several questions, issue a few high-signal searches (or one then refine), not near-duplicate parallel queries for every line item.
- **Hit fields:** `score` is normalized per query (top hit ≈ 1.0). Optional `cosine` (ANN; < 0.45 ≈ weak semantic match), `bm25` (FTS; more negative ≈ stronger lexical match), and `signals` (`fts` / `ann` / `graph`).
- **After hits:** `read_file` hits until the normalized score drops sharply (typically the top 3–5), including rank-1 even if the name looks unexpected. Skip only obvious noise (lockfiles). If the top hits all look off-topic, reformulate once; if two reformulations fail, fall back to `grep` / `glob`.
- **Answer only after reading:** a `search` snippet is never sufficient evidence — snippets truncate and can mislead. Before emitting an answer or `Evidence:` line, `read_file` the file you will cite and confirm the exact source lines (for binary assets like images/PDFs, `glob` existence is enough). Never emit a file path you have not confirmed via `read_file` / `glob` this session; if still unknown, write `Evidence: unknown`.
- **Long multi-item tasks:** when a task has more than ~10 enumerable items (question lists, checklists), write incremental results to a workspace file early (e.g. `answers.md`) and update it as you go. Do not wait until the end — context may be compacted mid-task.
- `list_skills` — resolves the skills root path for installed skills listed under `## Skills` below; read each skill's `SKILL.md` under that root for procedure.
- `run_command` — allowlisted shell with optional `timeout` (seconds). {run_command_exec_note} Shell starts in **workspace root**; use the paths listed under Runtime paths below — do NOT guess directory names. `cd` is a shell builtin and cannot be used as a bare command; use `bash -c "cd <dir> && <cmd>"` instead. Pass **`argv` as a list** with the binary first (e.g. `{{"argv": ["ls", "."]}}`); do not pass `{{"command": "ls -R", "args": []}}` — that treats `ls -R` as the binary name.
- `enable_mcp` / `disable_mcp` — connect or drop a server declared in mcp.json by name (e.g. `browser`). Success returns connection status + tools; failure returns the error (no separate status tool). New tools appear on the **next model step this turn**.
- `list_mcp_resources` / `read_mcp_resource` / `list_mcp_prompts` / `get_mcp_prompt` — appear only after `enable_mcp` succeeds; browse MCP resources and prompt templates from connected servers.
- `enable_loops` — advertise scheduled-loop tools (`start_loop`, `loop_status`, `pause_loop`, `resume_loop`, `stop_loop`, `disable_loops`); they appear only after `enable_loops`, on the **next model step this turn**. Requires durable storage (`DB_URL`) and a running scheduler worker. Read the `loop` skill for procedure (guards, confirmation) before calling `start_loop`.
{catalog_mcp_line}{catalog_loops_line}{web_search_line}{todo_list_line}{task_line}
### Workspace deliverables
- **New file or full rewrite** → `write_file`.
- **Targeted change to an existing file** → `read_file` then `replace_in_file` (unique match; light fuzzy fallbacks; optional `replace_all`).
- **Multi-file or multi-hunk edit** → `apply_patch` with a Codex-style `*** Begin Patch` … `*** End Patch` envelope (Add / Update / Delete / Move); fail-closed.
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
- memory storage: `{memory_storage_uri}` — optional notes live here; `search` indexes **workspace files** plus notes. Prefer `search` / `search_memory` over shelling into this URI; use the URI in `run_command` only for low-level inspection.

### MCP tools
- Names look like `server__tool` (double underscore).
- Heavy MCP servers are **on-demand**: call `enable_mcp("name")` before using their `server__*` tools when they are not yet in the active tool list.
- For server-published context (not callable tools), resource/prompt tools (`list_mcp_resources` / `read_mcp_resource`, `list_mcp_prompts` / `get_mcp_prompt`) appear in the tool list only after `enable_mcp`.
- MCP tool errors are returned as plain error text (not structured JSON). Any response containing an HTTP error code (4xx / 5xx), "not found", "unauthorized", "forbidden", "permission denied", or similar access/availability signals means the tool **did not return usable data**.
- When an MCP tool fails: state what failed in one sentence, then stop — do **not** fabricate, infer, or summarize content that the tool was supposed to fetch. If a fallback tool is available and meaningfully different, try it once; otherwise tell the user what is needed to proceed (e.g. correct credentials, a public URL, pasting the content directly).
- Scheduled loops: agree the tick plan with the user, then call `start_loop` (requires confirmation); prefer `max_ticks`/`max_runtime` over `unbounded`. Put soft stop criteria in the tick `prompt` and call `stop_loop` when met. Call `disable_loops` when finished.

### Skills
- Installed skill names are listed under `## Skills` in this prompt. When a task matches one, use `list_skills` to get the skills root, then `read_file` on that skill's `SKILL.md` for procedure before running commands or steps it documents."""


_TASK_LINE = (
    "- `task` — subprocess subagent with the same workspace, memory, and MCP configuration; "
    "pass `subagent_type` to select a named persona (see Subagent personas below). "
    "Returns JSON (summary, errors, usage). Nested `task` is disabled inside a subagent.\n"
)

_WEB_SEARCH_LINE = (
    "- `web_search` — search the web for current information; "
    "returns titles, URLs, and text snippets.\n"
)

_TODO_LIST_LINE = (
    "- `todo_list` — maintain an ordered, session-scoped task list "
    "(`add` / `complete` / `remove`). The live list appears under `## Todo list` "
    "when non-empty; keep it updated as you work.\n"
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
    include_todo_list: bool = False,
    workspace_root: str = "(not set)",
    memory_storage_uri: str = "(not set)",
    run_command_opensandbox: bool = False,
    subagent_personas: Sequence[tuple[str, str]] | None = None,
    emission_style: bool = False,
    catalog_mcp_servers: Sequence[str] | None = None,
    scheduled_loops_available: bool = False,
) -> str:
    """Runtime-owned description of core tools, paths, MCP naming, and strict tool-call rules.

    ``workspace_root`` and ``memory_storage_uri`` are injected once at
    context-build time so the model always uses correct paths in shell commands.
    ``include_web_search`` should be True when a web search backend is active.
    ``include_todo_list`` should be True when the session-scoped ``todo_list`` tool is active.
    ``run_command_opensandbox`` should match whether ``run_command`` is routed through
    OpenSandbox (same signal as ``SandboxConfig.from_env().enabled``).
    ``subagent_personas`` lists configured named subagent types for the parent orchestrator.
    ``emission_style`` opts in the terse emission-guidance block (env
    ``MONKEYBOT_EMISSION_STYLE``); the dense agent-to-agent sub-block is also gated
    on ``include_task_tool`` so it only appears when the ``task`` tool is active.
    ``catalog_mcp_servers`` lists mcp.json servers available via ``enable_mcp`` but not
    connected until the model activates them.
    ``scheduled_loops_available`` adds a catalog hint when durable loop storage is wired.
    """
    exec_note = _RUN_COMMAND_EXEC_NOTE_SANDBOX if run_command_opensandbox else _RUN_COMMAND_EXEC_NOTE_HOST
    catalog = [n.strip() for n in (catalog_mcp_servers or ()) if n and str(n).strip()]
    if catalog:
        names = ", ".join(f"`{n}`" for n in catalog)
        catalog_mcp_line = (
            f"- Configured MCP servers (call `enable_mcp` before use): {names}.\n"
        )
    else:
        catalog_mcp_line = ""
    catalog_loops_line = (
        "- Scheduled loops available (call `enable_loops` before use): "
        "prompt-first ticks via the scheduler worker.\n"
        if scheduled_loops_available
        else ""
    )
    body = _HARNESS_BODY.format(
        run_command_exec_note=exec_note,
        catalog_mcp_line=catalog_mcp_line,
        catalog_loops_line=catalog_loops_line,
        web_search_line=_WEB_SEARCH_LINE if include_web_search else "",
        todo_list_line=_TODO_LIST_LINE if include_todo_list else "",
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
