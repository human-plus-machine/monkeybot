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
from monkeybot.core.attachments.tools import read_attachment_tool_def, render_image_tool_def
from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.types.types_tools import ToolDef


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

    def is_pending_or_terminal(self, pending_key: str) -> Literal["pending", "terminated", "unknown"]: ...

    def abandon_pending_timeout(self, pending_key: str) -> None: ...

    def abandon_pending_cancel_all(self) -> None: ...


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
    """Workspace root for spill cleanup; optional for tests / minimal harness."""
    memory: MemorySubsystem | None = None
    """Memory subsystem for index refresh and search; optional when memory is disabled."""
    context_curation_enabled: bool = True
    """When True (parent agent), optional LLM curation may narrow memory in the system prompt."""
    sse_bus: PendingResponseBusPort | None = None
    """Gateway session bus for Story 5 pending UI responses; None for CLI / harness."""
    subagent_personas: tuple[tuple[str, str], ...] = ()
    """Named subagent types (name, description) advertised to the parent in the harness."""
    catalog_mcp_servers: tuple[str, ...] = ()
    """Configured MCP server names available via ``enable_mcp`` (not connected until activated)."""


_log = logging.getLogger(__name__)

# Built-in tools that mutate the live MCP registry; loop refreshes ctx.tools after these.
MCP_REGISTRY_MUTATING_TOOLS = frozenset(
    {
        "add_mcp_server",
        "remove_mcp_server",
        "enable_mcp",
        "disable_mcp",
    }
)


def refresh_tools_after_mcp_change(
    ctx: TurnContext,
    mcp_client: Any,
) -> TurnContext:
    """Rebuild ``ctx.tools`` after add/remove/enable/disable MCP (same user turn).

    Drops tools prefixed with any known MCP server name (catalog + ever-connected),
    then appends the current ``mcp_client.all_tools()`` snapshot.

    Mutates ``ctx.tools`` in place (frozen dataclass allows mutating the list) so
    callers that hold the same ``TurnContext`` — including the realtime gateway —
    observe the update without needing a returned replacement object.
    """
    prefixes = set(mcp_client.known_server_names())
    kept = [
        t
        for t in ctx.tools
        if not any(t.name.startswith(f"{prefix}__") for prefix in prefixes)
    ]
    ctx.tools[:] = kept + list(mcp_client.all_tools())
    return ctx


def _core_tool_defs(
    *,
    include_task_tool: bool = True,
    subagent_type_names: Sequence[str] | None = None,
) -> list[ToolDef]:
    """Static core tools always available before MCP extensions."""
    read_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative path under the workspace root."},
            "offset": {"type": "integer", "description": "1-based start line (optional)."},
            "limit": {"type": "integer", "description": "Max lines to return (optional)."},
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
            "path": {"type": "string", "description": "Repo-relative path under the workspace root."},
            "old_string": {
                "type": "string",
                "description": "Exact substring to replace (must match once).",
            },
            "new_string": {"type": "string", "description": "Replacement text (may be empty)."},
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
    search_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "q": {"type": "string"},
            "max_hits": {"type": "integer"},
        },
        "required": [],
    }
    run_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}},
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "arguments": {"type": "array", "items": {"type": "string"}},
            "shell": {"type": "string"},
            "script": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": [],
    }
    mcp_add_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "server_name": {"type": "string"},
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "env": {"type": "object"},
        },
        "required": [],
    }
    mcp_rm_schema: dict[str, object] = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "server_name": {"type": "string"}},
        "required": [],
    }
    enable_mcp_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Server name from mcp.json (e.g. browser).",
            },
            "server_name": {"type": "string"},
            "server": {"type": "string"},
        },
        "required": [],
    }
    disable_mcp_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "server_name": {"type": "string"},
            "server": {"type": "string"},
        },
        "required": [],
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
    }
    type_names = sorted({n.strip() for n in (subagent_type_names or []) if n and str(n).strip()})
    subagent_type_schema: dict[str, object] = {
        "type": "string",
        "description": (
            "Named subagent persona from monkeybot.yaml subagents registry. "
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
            "Run an allowlisted shell command with repo-scoped path checks (see TerminalExecutor).",
            run_schema,
        ),
        ToolDef(
            "read_file",
            "Read a UTF-8 text file from the workspace with path validation.",
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
            "Replace exactly one occurrence of old_string with new_string in an existing file. "
            "Fails if old_string is missing or matches more than once.",
            replace_schema,
        ),
        ToolDef(
            "glob",
            "List workspace file paths matching a glob pattern. Prefer over run_command+ls for discovery.",
            glob_schema,
            parallel_safe=True,
        ),
        ToolDef(
            "search_memory",
            "Search markdown/text under the memory directory for a keyword or phrase.",
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
                "summary, errors, and usage. Nested task calls are disabled inside the subagent.",
                task_schema,
            ),
        )
    tools.extend(
        [
            ToolDef(
                "enable_mcp",
                "Connect a configured MCP server by name from mcp.json (e.g. browser). "
                "Prefer this over add_mcp_server for known servers. New tools appear on "
                "the next model step this turn.",
                enable_mcp_schema,
            ),
            ToolDef(
                "disable_mcp",
                "Disconnect a connected MCP server by name and drop its tools from the "
                "next model step this turn.",
                disable_mcp_schema,
            ),
            ToolDef(
                "add_mcp_server",
                "Connect an ad-hoc MCP stdio server by name/command; tools appear as "
                "name__tool on the next model step this turn. Prefer enable_mcp for "
                "servers already listed in mcp.json.",
                mcp_add_schema,
            ),
            ToolDef(
                "remove_mcp_server",
                "Disconnect an MCP server by name and drop its tools (next model step).",
                mcp_rm_schema,
            ),
            ToolDef(
                "start_loop",
                "Start a prompt-first scheduled loop after the user confirms. Pass the agreed "
                "plan in prompt (BUSINESS/RULES). The scheduler fires that prompt on each tick. "
                "Requires durable storage (DB_URL).",
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
    extra_tools: Sequence[CustomTool] | None = None,
    subagent_registry: dict[str, SubagentConfig] | None = None,
) -> TurnContext:
    """Assemble a TurnContext from filesystem paths and the MCP client snapshot.

    Args:
        thread_id: Conversation thread id.
        request_id: Per-request correlation id.
        agent_md_path: Path to AGENT.md (must be non-empty).
        memory: Optional memory subsystem; when set, ``INDEX.md`` is loaded via storage.
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
        workspace_root: Optional workspace root for spill directory cleanup at run start.
        enable_context_curation: When False (e.g. subagent), skip LLM context curation for prompts.
        extra_tools: Optional list of in-process :class:`CustomTool` implementations.
            Their ``tool_def`` is appended to the tool list advertised to the model and
            their ``execute`` method is dispatched by :class:`CoreToolExecutor`.
        subagent_registry: Optional map of named subagent personas from monkeybot.yaml.

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
        tools.append(render_image_tool_def())
        tools.append(read_attachment_tool_def())
    tools.extend(mcp_client.all_tools())
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
        subagent_personas=personas,
        catalog_mcp_servers=catalog_names,
    )


async def refresh_memory_index(ctx: TurnContext) -> TurnContext:
    """Re-read ``INDEX.md`` via ``ctx.memory``; on failure return ``ctx`` unchanged."""
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
