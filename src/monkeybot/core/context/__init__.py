"""Per-turn context assembly: base prompt file (AGENT.md), memory index, skills, tools."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml

from monkeybot.core.attachments.config import attachments_enabled_from_env
from monkeybot.core.attachments.tools import load_file_tool_def
from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.events import AgentEvent
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.tools.workspace_service import AGENT_READ_DEFAULT_LINES
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.todo_list.store import TodoListStore


@runtime_checkable
class CustomTool(Protocol):
    """Protocol for lightweight in-process tools added by framework users.

    Implement this to add any tool that doesn't warrant a full MCP server.
    The ``tool_def`` is advertised to the model; ``execute`` is called by
    :class:`~monkeybot.core.tools.core_tool_executor.CoreToolExecutor` when the
    model invokes the tool by name.

    Example::

        class MyTool:
            tool_def = ToolDef(
                "my_lookup",
                "Look up something.",
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            )

            async def execute(self, args: dict) -> str:
                return f"result for {args['query']}"
    """

    tool_def: ToolDef

    async def execute(self, args: dict[str, object]) -> str | ToolExecutionResult: ...


@runtime_checkable
class PendingResponseBusPort(Protocol):
    """Minimal gateway bus surface for Story 5 pending UI responses (keeps core free of gateway imports)."""

    pending_responses: dict[str, asyncio.Future[Any]]

    def register_pending(self, pending_key: str) -> asyncio.Future[Any]: ...

    def resolve_pending(self, pending_key: str, payload: Any) -> bool: ...

    def is_pending_or_terminal(
        self, pending_key: str
    ) -> Literal["pending", "terminated", "unknown"]: ...

    def abandon_pending_timeout(self, pending_key: str) -> None: ...

    def abandon_pending_cancel_all(self) -> None: ...


@runtime_checkable
class EventPublisherPort(Protocol):
    """Minimal sink for parent-session AgentEvents (keeps core free of gateway imports)."""

    async def publish_event(self, event: AgentEvent) -> None:
        """Serialize and publish onto the parent session SSE bus."""
        ...


@dataclass(frozen=True)
class SkillRef:
    """Discovered skill metadata for prompting and listing.

    Attributes:
        name: Directory name under the skills root.
        description: First instructional line from SKILL.md (after optional frontmatter).
    """

    name: str
    description: str


@dataclass(frozen=True)
class TurnContext:
    """Immutable bundle of everything needed for one agent turn."""

    thread_id: str
    request_id: str
    agent_md: str
    memory_index: list[str]
    skills: list[SkillRef]
    tools: list[ToolDef]
    user_id: str | None
    parent_run_id: str | None
    model: str
    summarization_model: str | None = None
    """Optional model id for history compression; env ``CONTEXT_SUMMARIZATION_MODEL`` overrides when set."""
    cancelled: asyncio.Event | None = None
    """When set by the parent run (e.g. gateway Stop), tool code may stop side effects early."""
    context_window_tokens: int = 200_000
    """Max input context for pre-flight checks; summarization triggers near this cap."""
    workspace_root: Path | None = None
    """Workspace root for tools and spill file paths (from ``paths.workspace_root``); optional for tests."""
    memory: MemorySubsystem | None = None
    """Memory subsystem for index refresh and search; optional when memory is disabled."""
    context_curation_enabled: bool = True
    """When True (parent agent), optional LLM curation may narrow memory in the system prompt."""
    sse_bus: PendingResponseBusPort | None = None
    """Gateway session bus for Story 5 pending UI responses; None for CLI / harness."""
    event_publisher: EventPublisherPort | None = None
    """Optional parent SSE publisher for nested subagent progress; None for CLI / tests."""
    subagent_personas: tuple[tuple[str, str], ...] = ()
    """Named subagent types (name, description) advertised to the parent in the harness."""
    catalog_mcp_servers: tuple[str, ...] = ()
    """Configured MCP server names available via ``enable_mcp`` (not connected until activated)."""
    scheduled_loops_available: bool = False
    """True when durable loop storage is wired (DB_URL); advertise ``enable_loops`` catalog hint."""
    todo_store: TodoListStore | None = None
    """Session-scoped todo list (parent agent only); mutable store held by frozen context."""


_log = logging.getLogger(__name__)

# Built-in tools that mutate the live MCP registry; loop refreshes ctx.tools after these.
MCP_REGISTRY_MUTATING_TOOLS = frozenset(
    {
        "enable_mcp",
        "disable_mcp",
    }
)

# Built-in tools that mutate scheduled-loop tool advertisement; loop refreshes ctx.tools.
LOOPS_REGISTRY_MUTATING_TOOLS = frozenset(
    {
        "enable_loops",
        "disable_loops",
    }
)


@dataclass
class LoopsToolRegistry:
    """Process-local progressive-loop advertisement (mirrors MCP client connection state).

    Gateway deps hold one instance for the process lifetime so ``enable_loops`` sticks
    across user turns until ``disable_loops`` (or process restart).

    Not shared across gateway replicas: multi-instance deploys need a single replica
    or sticky routing so enable/disable stays consistent for a session (same tradeoff
    as in-process MCP connections).
    """

    advertised: bool = False


# Resource/prompt meta-tools — advertised only after an MCP server is connected.
_MCP_SERVER_FILTER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": "Optional MCP server name. Omit to query all connected servers.",
        },
    },
    "required": [],
}
MCP_PROGRESSIVE_META_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        "list_mcp_resources",
        "List MCP resources from connected servers. Optional server filter.",
        _MCP_SERVER_FILTER_SCHEMA,
        parallel_safe=True,
    ),
    ToolDef(
        "read_mcp_resource",
        "Read one MCP resource by server name and URI from list_mcp_resources.",
        {
            "type": "object",
            "properties": {
                "server": {
                    "type": "string",
                    "description": "MCP server name from list_mcp_resources.",
                },
                "uri": {
                    "type": "string",
                    "description": "Resource URI from list_mcp_resources.",
                },
            },
            "required": ["server", "uri"],
        },
        parallel_safe=True,
    ),
    ToolDef(
        "list_mcp_prompts",
        "List MCP prompt templates from connected servers. Optional server filter.",
        _MCP_SERVER_FILTER_SCHEMA,
        parallel_safe=True,
    ),
    ToolDef(
        "get_mcp_prompt",
        "Fetch a named MCP prompt template (optional string arguments) from a connected server.",
        {
            "type": "object",
            "properties": {
                "server": {
                    "type": "string",
                    "description": "MCP server name from list_mcp_prompts.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Prompt name from list_mcp_prompts.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Optional string arguments for the prompt template.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["server", "prompt"],
        },
        parallel_safe=True,
    ),
)
MCP_PROGRESSIVE_META_TOOLS = frozenset(t.name for t in MCP_PROGRESSIVE_META_TOOL_DEFS)

# Lifecycle tools — advertised only after ``enable_loops`` (or auto-advertise).
SCHEDULED_LOOP_TOOL_DEFS: tuple[ToolDef, ...] = (
    ToolDef(
        "start_loop",
        "Start a prompt-first scheduled loop after the user confirms. Pass the agreed "
        "plan in prompt (BUSINESS/RULES). The scheduler fires that prompt on each tick. "
        "Requires durable storage (DB_URL). Call ``enable_loops`` first when these tools "
        "are not yet in the active tool list.",
        {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Agreed loop plan / tick instructions.",
                },
                "interval": {
                    "type": "string",
                    "description": "Tick interval, e.g. 20s, 5m, 1h.",
                },
                "loop_id": {"type": "string"},
                "session_id": {
                    "type": "string",
                    "description": "Conversation thread for ticks (default loop-main).",
                },
                "max_ticks": {"type": "integer"},
                "max_runtime": {
                    "type": "string",
                    "description": "Hard wall-clock limit, e.g. 1h.",
                },
                "unbounded": {
                    "type": "boolean",
                    "description": (
                        "Opt out of max_ticks/max_runtime guards. "
                        "Requires explicit user confirmation."
                    ),
                },
                "skip_if_busy": {"type": "boolean"},
            },
            "required": ["prompt", "interval"],
        },
    ),
    ToolDef(
        "loop_status",
        "Get status of one scheduled loop or list all loops.",
        {
            "type": "object",
            "properties": {"loop_id": {"type": "string"}},
            "required": [],
        },
        parallel_safe=True,
        doom_loop_exempt=True,
    ),
    ToolDef(
        "pause_loop",
        "Pause a scheduled loop.",
        {
            "type": "object",
            "properties": {"loop_id": {"type": "string"}},
            "required": ["loop_id"],
        },
    ),
    ToolDef(
        "resume_loop",
        "Resume a paused scheduled loop.",
        {
            "type": "object",
            "properties": {"loop_id": {"type": "string"}},
            "required": ["loop_id"],
        },
    ),
    ToolDef(
        "stop_loop",
        "Stop a scheduled loop permanently.",
        {
            "type": "object",
            "properties": {"loop_id": {"type": "string"}},
            "required": ["loop_id"],
        },
    ),
    ToolDef(
        "disable_loops",
        "Drop scheduled-loop tools from the next model step this turn. "
        "Running loops keep their scheduler state; call `stop_loop` first to "
        "end them.",
        {"type": "object", "properties": {}, "required": []},
    ),
)
SCHEDULED_LOOP_TOOL_NAMES = frozenset(t.name for t in SCHEDULED_LOOP_TOOL_DEFS)


def _any_mcp_connected(mcp_client: Any) -> bool:
    """True when any known MCP server has an active session."""
    return any(mcp_client.is_connected(name) for name in mcp_client.known_server_names())


def refresh_tools_after_mcp_change(
    ctx: TurnContext,
    mcp_client: Any,
) -> TurnContext:
    """Rebuild ``ctx.tools`` after enable/disable MCP (same user turn).

    Drops tools prefixed with any known MCP server name (catalog + ever-connected)
    and progressive MCP meta-tools, then appends the current ``mcp_client.all_tools()``
    snapshot plus resource/prompt meta-tools when any server is connected.

    Mutates ``ctx.tools`` in place (frozen dataclass allows mutating the list) so
    callers that hold the same ``TurnContext`` — including the realtime gateway —
    observe the update without needing a returned replacement object.
    """
    prefixes = set(mcp_client.known_server_names())
    kept = [
        t
        for t in ctx.tools
        if t.name not in MCP_PROGRESSIVE_META_TOOLS
        and not any(t.name.startswith(f"{prefix}__") for prefix in prefixes)
    ]
    rebuilt = kept + list(mcp_client.all_tools())
    if _any_mcp_connected(mcp_client):
        rebuilt.extend(MCP_PROGRESSIVE_META_TOOL_DEFS)
    ctx.tools[:] = rebuilt
    return ctx


def refresh_tools_after_loops_change(
    ctx: TurnContext,
    *,
    loops_advertised: bool,
) -> TurnContext:
    """Rebuild ``ctx.tools`` after enable/disable loops (same user turn).

    Drops scheduled-loop progressive tools (lifecycle + ``disable_loops``), then
    re-appends them when advertised. ``enable_loops`` stays in the core set.
    Mutates ``ctx.tools`` in place.
    """
    kept = [t for t in ctx.tools if t.name not in SCHEDULED_LOOP_TOOL_NAMES]
    if loops_advertised:
        kept.extend(SCHEDULED_LOOP_TOOL_DEFS)
    ctx.tools[:] = kept
    return ctx


def _core_tool_defs(
    *,
    include_task_tool: bool = True,
    subagent_type_names: Sequence[str] | None = None,
) -> list[ToolDef]:
    """Static core tools always available before MCP extensions."""
    default_lines = AGENT_READ_DEFAULT_LINES
    read_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative path under the workspace root.",
            },
            "offset": {
                "type": "integer",
                "description": (
                    "1-based start line (optional). Continue a truncated read from "
                    "the payload's next_offset — do not page the file in tiny chunks."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Max lines to return. Defaults to {default_lines} when omitted; "
                    f"prefer omitting limit (or >= {default_lines}) over many small reads. "
                    f"Pass a larger value when you need more of the file. Large reads are "
                    f"additionally bounded by a context-derived char budget; check "
                    f"truncated / next_offset. Avoid small limits (e.g. 40) with repeated "
                    f"read_file calls — one larger read is cheaper."
                ),
            },
        },
        "required": ["path"],
    }
    write_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "Full file contents (may be empty)."},
        },
        "required": ["path"],
    }
    replace_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative path under the workspace root.",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Substring to replace. Prefer an exact unique match; the tool also "
                    "tries light fuzzy matching (line-trim / whitespace) when exact fails."
                ),
            },
            "new_string": {"type": "string", "description": "Replacement text (may be empty)."},
            "replace_all": {
                "type": "boolean",
                "description": "When true, replace every match (default false: require a unique match).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    glob_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. **/*.html, *.md).",
            },
            "root": {
                "type": "string",
                "description": "Optional repo-relative directory to search under.",
            },
        },
        "required": ["pattern"],
    }
    grep_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Python regex to search for in file contents. Prefer simple patterns: "
                    "ripgrep may accelerate the search when available, but constructs that "
                    "need Python re (lookaround, backreferences) always use the Python engine."
                ),
            },
            "root": {
                "type": "string",
                "description": (
                    "Optional repo-relative directory or file to search under "
                    "(default workspace root). A file path searches just that file."
                ),
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (default false).",
            },
            "file_glob": {
                "description": (
                    'Optional filename filter: a string (e.g. "*.py", "*.{ts,tsx}") or a list '
                    "of globs. Brace expansion is supported. An unparseable glob is an error "
                    "(never a silent empty match)."
                ),
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "max_matches": {
                "type": "integer",
                "description": (
                    "Cap on returned matches in this page (default server limit). "
                    "Does not stop the scan; check total_match_count and next_offset."
                ),
            },
            "offset": {
                "type": "integer",
                "description": (
                    "Skip this many matches before collecting the returned page "
                    "(default 0). Continue from a prior response's next_offset."
                ),
            },
        },
        "required": ["pattern"],
    }
    apply_patch_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "patch_text": {
                "type": "string",
                "description": (
                    "Full Codex-style patch between *** Begin Patch and *** End Patch, "
                    "with *** Add File: / *** Update File: / *** Delete File: sections."
                ),
            },
        },
        "required": ["patch_text"],
    }
    search_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "One focused conceptual query (distinctive nouns). "
                    "Avoid dumping many near-duplicate questions in parallel. "
                    "Not for past conversations — use `mempalace search`."
                ),
            },
            "q": {"type": "string"},
            "path_prefix": {
                "type": "string",
                "description": "Optional path filter (workspace-relative or notes/).",
            },
            "source": {
                "type": "string",
                "enum": ["any", "note", "workspace_file"],
                "description": "Filter by provenance. Default any.",
            },
            "limit": {"type": "integer", "description": "Max hits (default ~10)."},
            "max_hits": {"type": "integer"},
        },
        "required": [],
    }
    run_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Command as [binary, ...args]. Preferred over a combined command string."
                ),
            },
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "arguments": {"type": "array", "items": {"type": "string"}},
            "shell": {"type": "string"},
            "script": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": [],
    }
    enable_mcp_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Server name from mcp.json (e.g. browser).",
            },
        },
        "required": ["name"],
    }
    disable_mcp_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Connected MCP server name to disconnect.",
            },
        },
        "required": ["name"],
    }
    list_skills_schema: dict[str, object] = {"type": "object", "properties": {}}
    task_props: dict[str, object] = {
        "task": {
            "type": "string",
            "description": "Focused objective or question for the subagent to complete.",
        },
        "context": {
            "type": "string",
            "description": "Optional background the parent already gathered (constraints, paths, prior tool output).",
        },
        "expect_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Workspace-relative files the subagent is expected to create or update. "
                "The result reports artifact_exists plus a per-path artifacts list, so you "
                "do not need to check with a follow-up read_file or glob."
            ),
        },
    }
    type_names = sorted({n.strip() for n in (subagent_type_names or []) if n and str(n).strip()})
    subagent_type_schema: dict[str, object] = {
        "type": "string",
        "description": (
            "Named subagent persona from monkeybot.yaml subagents.personas. "
            "Omit to use the default subagent AGENT.md."
        ),
    }
    if type_names:
        subagent_type_schema["enum"] = type_names
    task_props["subagent_type"] = subagent_type_schema
    task_schema: dict[str, object] = {
        "type": "object",
        "properties": task_props,
        "required": ["task"],
    }
    tools: list[ToolDef] = [
        ToolDef(
            "run_command",
            (
                "Run an allowlisted shell command with optional timeout. "
                'Pass argv as a list with the binary first (e.g. ["ls", "."]); '
                "do not pass a combined string as the binary. Shell starts in "
                "workspace root. cd is a builtin and is not a valid command — "
                "pass workspace-relative paths to the binary instead."
            ),
            run_schema,
        ),
        ToolDef(
            "read_file",
            (
                f"Read a UTF-8 text file from the workspace with path validation. "
                f"Without limit, returns up to {default_lines} lines from offset. "
                f"Prefer that default (or a larger limit) over many small reads; "
                f"use offset+limit only to continue from next_offset when truncated."
            ),
            read_schema,
            parallel_safe=True,
        ),
        ToolDef(
            "write_file",
            "Write or replace a UTF-8 text file under the workspace root.",
            write_schema,
        ),
        ToolDef(
            "replace_in_file",
            "Replace old_string with new_string in an existing file. Requires a unique match "
            "unless replace_all is true. Uses exact match first, then light fuzzy fallbacks.",
            replace_schema,
        ),
        ToolDef(
            "glob",
            "List workspace file paths matching a glob pattern. Prefer over run_command+ls for "
            "discovery. For content questions ('how does X work?'), use `search` first. "
            "A path list is evidence of absence only when the call succeeds with ok:true "
            "(incomplete scans return ok:false / incomplete_scan — narrow root or pattern).",
            glob_schema,
            parallel_safe=True,
        ),
        ToolDef(
            "grep",
            "Search workspace file contents with a Python regex. Prefer over run_command+grep. "
            "Best for exact identifiers; for conceptual / paraphrased questions, use `search` first. "
            "An empty match list is evidence of absence only when the payload has "
            "scan_complete=true (incomplete scans return ok:false / incomplete_scan — narrow "
            "root or pass file_glob). Capped pages still report total_match_count and next_offset.",
            grep_schema,
            parallel_safe=True,
        ),
        ToolDef(
            "apply_patch",
            "Apply a multi-file Codex-style patch (Add / Update / Delete / Move). "
            "Fail-closed: nothing is written if any hunk fails to validate.",
            apply_patch_schema,
        ),
        ToolDef(
            "search",
            "Search the local workspace index (source files + knowledge notes) via "
            "keyword FTS, link graph, and optional embeddings. Has no record of past "
            "conversations — use `mempalace search` for those. Default first step for "
            "unfamiliar code / conceptual / paraphrased / cross-file questions. "
            "Hits return normalized score (top≈1.0), optional cosine/bm25/signals; "
            "read until the score drops sharply (top 3–5). For locate-a-file/asset "
            "questions prefer `glob`. Prefer `grep` for exact identifiers.",
            search_schema,
            parallel_safe=True,
        ),
        ToolDef(
            "list_skills",
            "List installed skills with names, descriptions, and entry points.",
            list_skills_schema,
            parallel_safe=True,
        ),
    ]
    if include_task_tool:
        tools.append(
            ToolDef(
                "task",
                "Spawn a subprocess subagent with the same workspace, memory, and MCP configuration "
                "to work on a delegated objective. Pass subagent_type to select a named persona "
                "(see harness Subagent personas). Returns JSON with the subagent's streamed answer "
                "summary, errors, and usage. When queue mode returns ok:false / error_kind:pending "
                "/ queued:true, the child has not finished — do not treat that as completion. "
                "Nested task calls are disabled inside the subagent.",
                task_schema,
            ),
        )
    tools.extend(
        [
            ToolDef(
                "enable_mcp",
                "Connect a configured MCP server by name from mcp.json (e.g. browser). "
                "On success returns connection status and discovered tools; on failure "
                "returns the error (no separate status check needed). New server tools "
                "and MCP resource/prompt tools appear on the next model step this turn.",
                enable_mcp_schema,
            ),
            ToolDef(
                "disable_mcp",
                "Disconnect a connected MCP server by name and drop its tools from the "
                "next model step this turn.",
                disable_mcp_schema,
            ),
            ToolDef(
                "enable_loops",
                "Advertise scheduled-loop tools (`start_loop`, `loop_status`, "
                "`pause_loop`, `resume_loop`, `stop_loop`, `disable_loops`) on the "
                "next model step this turn. Requires durable storage (DB_URL). "
                "Prefer the loop skill for procedure before starting a loop.",
                {"type": "object", "properties": {}, "required": []},
            ),
        ]
    )
    return tools


def _first_body_line(lines: list[str], start: int, skill_name: str) -> str:
    """Return the first non-empty body line, stripping a leading markdown heading."""
    i = start
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return f"Skill {skill_name}"
    line = lines[i].strip()
    if line.startswith("#"):
        line = line.lstrip("#").strip()
    return line or f"Skill {skill_name}"


def _parse_skill_description(body: str, skill_name: str) -> str:
    """Return short description from SKILL.md YAML frontmatter or first body line."""
    lines = body.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return f"Skill {skill_name}"
    if lines[i].strip() != "---":
        return _first_body_line(lines, i, skill_name)

    frontmatter_end = i + 1
    while frontmatter_end < len(lines) and lines[frontmatter_end].strip() != "---":
        frontmatter_end += 1
    if frontmatter_end >= len(lines):
        return _first_body_line(lines, i, skill_name)

    frontmatter_text = "\n".join(lines[i + 1 : frontmatter_end])
    try:
        meta = yaml.safe_load(frontmatter_text)
        if isinstance(meta, dict):
            desc = meta.get("description")
            if isinstance(desc, str) and desc.strip():
                return desc.strip()
    except yaml.YAMLError:
        pass

    return _first_body_line(lines, frontmatter_end + 1, skill_name)


def _discover_skills(skills_path: Path) -> list[SkillRef]:
    """List skills as immediate subdirectories containing ``SKILL.md``."""
    if not skills_path.is_dir():
        return []

    result: list[SkillRef] = []
    for child in sorted(skills_path.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        desc = _parse_skill_description(text, child.name)
        result.append(SkillRef(name=child.name, description=desc))
    return result


def _load_agent_md(agent_md_path: Path) -> str:
    """Read AGENT.md; trailing newlines stripped; fail if blank."""
    raw = agent_md_path.read_text(encoding="utf-8")
    content = raw.rstrip("\n")
    if not content.strip():
        raise ValueError(f"AGENT.md is empty or whitespace-only: {agent_md_path}")
    return content


async def build_context(
    thread_id: str,
    request_id: str,
    *,
    agent_md_path: Path,
    memory: MemorySubsystem | None,
    skills_path: Path,
    mcp_client: MCPClientPort,
    user_id: str | None = None,
    parent_run_id: str | None = None,
    model: str = "gemini-2.5-flash",
    summarization_model: str | None = None,
    include_task_tool: bool = True,
    cancelled: asyncio.Event | None = None,
    context_window_tokens: int = 200_000,
    workspace_root: Path | None = None,
    enable_context_curation: bool = True,
    sse_bus: PendingResponseBusPort | None = None,
    event_publisher: EventPublisherPort | None = None,
    extra_tools: Sequence[CustomTool] | None = None,
    subagent_registry: dict[str, SubagentConfig] | None = None,
    scheduled_loops_available: bool = False,
    loops_advertised: bool = False,
    todo_store: TodoListStore | None = None,
) -> TurnContext:
    """Assemble a TurnContext from filesystem paths and the MCP client snapshot.

    Args:
        thread_id: Conversation thread id.
        request_id: Per-request correlation id.
        agent_md_path: Path to AGENT.md (must be non-empty).
        memory: Optional memory subsystem; when set, L0+L1 wake-up is loaded.
        skills_path: Root directory for skill folders (each with ``SKILL.md``).
        mcp_client: Client exposing ``all_tools()`` for MCP-registered tools.
        user_id: Optional authenticated user.
        parent_run_id: Optional parent run for subagent linkage.
        model: Model id for this turn.
        summarization_model: Optional model id for sync history summarization; overridden by
            ``CONTEXT_SUMMARIZATION_MODEL`` when that env var is non-empty.
        include_task_tool: When False, omit the ``task`` tool (used by the subagent worker).
        cancelled: Optional cooperative-cancel handle for the parent turn (gateway / CLI).
        context_window_tokens: Model context budget for pre-flight and summarization triggers.
        workspace_root: Optional workspace root for tools/spill paths (``paths.workspace_root``).
        enable_context_curation: When False (e.g. subagent), skip LLM context curation for prompts.
        sse_bus: Optional gateway bus for pending UI responses.
        event_publisher: Optional parent SSE publisher for nested subagent progress.
        extra_tools: Optional list of in-process :class:`CustomTool` implementations.
            Their ``tool_def`` is appended to the tool list advertised to the model and
            their ``execute`` method is dispatched by :class:`CoreToolExecutor`.
        subagent_registry: Optional map of named subagent personas from monkeybot.yaml.
        scheduled_loops_available: True when durable loop storage is wired.
        loops_advertised: True when the caller's ``LoopsToolRegistry`` has ``enable_loops``
            active; includes scheduled-loop lifecycle tools immediately for this turn.
        todo_store: Optional session-scoped todo list store (parent agent); enables volatile
            ``## Todo list`` injection. Pass the same store to ``TodoListTool`` via ``extra_tools``.

    Returns:
        Frozen :class:`TurnContext`.

    Raises:
        ValueError: When AGENT.md is missing or empty after normalization.
    """
    agent_md = _load_agent_md(agent_md_path)
    memory_index = await memory.load_index() if memory is not None else []
    skills = _discover_skills(skills_path)
    registry = subagent_registry or {}
    type_names = sorted(registry)
    personas = tuple((name, registry[name].description) for name in type_names)
    tools = list(
        _core_tool_defs(
            include_task_tool=include_task_tool,
            subagent_type_names=type_names,
        )
    )
    if attachments_enabled_from_env():
        tools.append(load_file_tool_def())
    tools.extend(mcp_client.all_tools())
    if _any_mcp_connected(mcp_client):
        tools.extend(MCP_PROGRESSIVE_META_TOOL_DEFS)
    if loops_advertised and scheduled_loops_available:
        tools.extend(SCHEDULED_LOOP_TOOL_DEFS)
    for ct in extra_tools or []:
        tools.append(ct.tool_def)
    catalog_names = tuple(mcp_client.catalog_names())
    return TurnContext(
        thread_id=thread_id,
        request_id=request_id,
        agent_md=agent_md,
        memory_index=memory_index,
        skills=skills,
        tools=tools,
        user_id=user_id,
        parent_run_id=parent_run_id,
        model=model,
        summarization_model=summarization_model,
        cancelled=cancelled,
        context_window_tokens=context_window_tokens,
        workspace_root=workspace_root,
        memory=memory,
        context_curation_enabled=enable_context_curation,
        sse_bus=sse_bus,
        event_publisher=event_publisher,
        subagent_personas=personas,
        catalog_mcp_servers=catalog_names,
        scheduled_loops_available=scheduled_loops_available,
        todo_store=todo_store,
    )


async def refresh_memory_index(ctx: TurnContext) -> TurnContext:
    """Re-read MemPalace wake-up via ``ctx.memory``; on failure return ``ctx`` unchanged."""
    if ctx.memory is None:
        return ctx

    try:
        fresh = await ctx.memory.load_index()
        return dataclasses.replace(ctx, memory_index=fresh)
    except Exception as exc:
        _log.warning(
            "[MEMORY] Index refresh FAILED — serving stale index. "
            "thread_id=%s memory_uri=%s error=%r",
            ctx.thread_id,
            ctx.memory.uri,
            exc,
        )
        return ctx
