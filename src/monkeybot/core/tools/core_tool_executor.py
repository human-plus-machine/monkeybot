"""Default tool executor: core filesystem, memory search, shell (allowlisted), and MCP."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shlex
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from monkeybot.core.attachments.config import (
    ALLOWED_MIME_TYPES,
    IMAGE_MIME_TYPES,
    max_image_bytes,
    max_pdf_bytes,
)
from monkeybot.core.attachments.store import AttachmentStore, sniff_mime
from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.context import CustomTool, TurnContext
from monkeybot.core.context.tool_result_ingress import (
    cap_tool_result_text,
    sanitize_tool_result_text,
    skip_tool_result_sanitize,
)
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.logging_utils import kv
from monkeybot.core.mcp.mcp_client import (
    MCPConnectionError,
    MCPDiagnosticError,
    MCPServerNotConnectedError,
)
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import RunStore, ScheduledLoopStore
from monkeybot.core.persistence.durable_runs import SubagentEnvelope as PersistedSubagentEnvelope
from monkeybot.core.persistence.scheduled_loops import ScheduledLoopCreate
from monkeybot.scheduler.interval import parse_interval_ms, parse_optional_duration_ms
from monkeybot.core.persistence.runs import make_run_id
from monkeybot.core.runtime.events import (
    AssistantDelta,
    Error,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
)
from monkeybot.core.runtime.loop import ToolExecutorPort
from monkeybot.core.subagents.subagent_proto import (
    SubagentEnvelope,
    normalize_sqlite_db_url,
    resolve_agent_project_root,
    resolve_project_path,
    resolve_subagent_script,
    resolve_task_agent_md_path,
    spawn_subagent,
)
from monkeybot.core.tools.sandbox_executor import SandboxConfig, SandboxExecutor
from monkeybot.core.tools.spill_inventory import spill_inventory_note, spill_min_chars_from_env
from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    SecurityError,
    TerminalExecutor,
)
from monkeybot.core.tools.types import ToolExecutionResult
from monkeybot.core.tools.workspace_service import (
    WorkspaceError,
    WorkspaceFileService,
    WorkspaceSettings,
)
from monkeybot.core.types.content_blocks import File, Image

logger = logging.getLogger(__name__)

_SUBAGENT_OTEL_SERVICE_NAME = "monkeybot-subagent"

_PARENT_CANCEL_TASK_ERR = "task: cancelled (parent)"

_SPILL_DIR = ".monkeybot/spill"

_CORE_TOOL_NAMES = frozenset(
    {
        "load_file",
        "read_file",
        "write_file",
        "replace_in_file",
        "glob",
        "grep",
        "apply_patch",
        "search_memory",
        "list_skills",
        "task",
        "run_command",
        "enable_mcp",
        "disable_mcp",
        "list_mcp_resources",
        "read_mcp_resource",
        "list_mcp_prompts",
        "get_mcp_prompt",
        "start_loop",
        "loop_status",
        "pause_loop",
        "resume_loop",
        "stop_loop",
    }
)

_SPILL_SKIP_TOOLS = frozenset({"read_file", "load_file"})

def _tool_handler_kind(name: str, *, mcp: MCPClientPort, extra_tools: dict[str, CustomTool]) -> str:
    if name in _CORE_TOOL_NAMES:
        return "core"
    if name in extra_tools:
        return "extra"
    if mcp.split_prefixed_tool(name) is not None:
        return "mcp"
    return "unknown"


def _safe_spill_filename(call_id: str) -> str:
    safe = "".join(c for c in call_id if c.isalnum() or c in "-_")[:200]
    return safe or "call"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def workspace_settings_from_env() -> WorkspaceSettings:
    """Build workspace read limits from harness env (yaml-backed via runtime_env)."""
    return WorkspaceSettings(
        WORKSPACE_READ_MAX_LINES=_int_env("MONKEYBOT_READ_MAX_LINES", 5000),
        WORKSPACE_READ_DEFAULT_LINES=_int_env("MONKEYBOT_READ_DEFAULT_LINES", 2000),
        WORKSPACE_SPILL_READ_MAX_LINES=_int_env("MONKEYBOT_SPILL_READ_MAX_LINES", 50_000),
    )


def _write_spill_with_inventory(
    text: str,
    workspace_root: Path,
    thread_id: str,
    call_id: str,
) -> str:
    """Write raw ``text`` to spill file; return inventory pointer only (not inline body)."""
    rel = f"{_SPILL_DIR}/{thread_id}/{_safe_spill_filename(call_id)}.txt"
    out_path = (Path(workspace_root) / rel).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return spill_inventory_note(text, rel)


def _is_under_spill_path(workspace_root: Path, rel_path: str) -> bool:
    s = str(rel_path).strip().replace("\\", "/").lstrip("/")
    if not s or ".." in s or s.startswith("~"):
        return False
    try:
        resolved = (Path(workspace_root) / s).resolve()
        resolved.relative_to(Path(workspace_root).resolve())
    except ValueError:
        return False
    spill_root = (Path(workspace_root).resolve() / _SPILL_DIR).resolve()
    try:
        resolved.relative_to(spill_root)
        return True
    except ValueError:
        return False


async def _stop_subagent_process(proc: asyncio.subprocess.Process | None) -> None:
    """SIGTERM then reap; SIGKILL if still alive after a short wait."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=8.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


def _j(data: object) -> str:
    return json.dumps(data, ensure_ascii=False)


def _built_in_tool_error(
    error_kind: str,
    message: str,
    hint: str,
    details: dict[str, Any] | None = None,
) -> str:
    """JSON tool-error body for built-in tools (helps the model recover without MCP wrapping)."""
    payload: dict[str, Any] = {
        "ok": False,
        "error_kind": error_kind,
        "message": message,
        "hint": hint,
    }
    if details:
        payload["details"] = details
    return _j(payload)


def _workspace_error_envelope(exc: WorkspaceError) -> str:
    code = getattr(exc, "code", "workspace_error")
    msg = str(exc)
    if code == "path_escape":
        hint = (
            "Use a path relative to the workspace root with no `..` or `~` "
            '(e.g. {"path": "README.md"}).'
        )
    elif code == "invalid_path":
        hint = "Remove `..`, `~`, and leading `/`; use a repo-relative path."
    elif code == "missing_path":
        hint = 'Include a non-empty "path" argument (workspace-relative).'
    elif code == "write_outside_scope":
        hint = "Write only under the configured write scope, or ask the operator to adjust policy."
    elif code == "not_found":
        hint = "Create the file with write_file first, or fix the path spelling."
    elif code == "not_found_replace":
        hint = "Re-read the file and provide an exact unique old_string (or rely on fuzzy match with more context)."
    elif code == "ambiguous_replace":
        hint = "Widen old_string until unique, or set replace_all=true."
    elif code == "invalid_offset":
        hint = 'Use "offset" as a positive integer (1 = first line).'
    elif code in ("write_failed", "glob_failed", "delete_failed"):
        hint = "Check disk permissions and path; retry after fixing the underlying issue."
    else:
        hint = "Fix the path or arguments per read_file/write_file rules, then retry once."
    return _built_in_tool_error("validation", msg, hint, {"code": code})


def _run_command_parse_envelope(exc: ValueError) -> str:
    return _built_in_tool_error(
        "validation",
        str(exc),
        'Use a non-empty argv list, e.g. {"argv": ["git", "--version"]} '
        'or {"command": "git", "args": ["--version"]}.',
        {"example": {"argv": ["git", "--version"]}},
    )


def _coerce_int(val: object | None, default: int | None = None) -> int | None:
    if val is None:
        return default
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str) and val.strip():
        s = val.strip()
        if s.lstrip("-").isdigit():
            return int(s)
    return default


def _inject_subagent_traceparent() -> str | None:
    try:
        from monkeybot.observability.propagation import inject_traceparent
    except ImportError:
        return None
    carrier: dict[str, str] = {}
    inject_traceparent(carrier)
    return carrier.get("traceparent")


def _str_arg(args: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


_SHELL_OPERATOR_TOKENS = frozenset({"&&", "||", ";", "|", "&"})


def _needs_shell_wrapper(stripped: str, parts: list[str]) -> bool:
    if any(part in _SHELL_OPERATOR_TOKENS for part in parts):
        return True
    return any(op in stripped for op in _SHELL_OPERATOR_TOKENS)


def _argv_from_command_string(stripped: str) -> tuple[str, list[str]]:
    """Parse a command string; wrap in ``bash -c`` when shell operators are present."""
    if not any(ch.isspace() for ch in stripped):
        parts = shlex.split(stripped, posix=True)
        if parts and _needs_shell_wrapper(stripped, parts):
            return "bash", ["-c", stripped]
        return stripped, []
    parts = shlex.split(stripped, posix=True)
    if not parts:
        return stripped, []
    if _needs_shell_wrapper(stripped, parts):
        return "bash", ["-c", stripped]
    return parts[0], parts[1:]


def _parse_run_command(args: dict[str, Any]) -> tuple[str, list[str]]:
    argv_raw = args.get("argv")
    if isinstance(argv_raw, list) and argv_raw:
        argv = [str(x) for x in argv_raw]
        return argv[0], argv[1:]

    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        extra = args.get("args")
        if extra is None:
            extra = args.get("arguments")
        if isinstance(extra, list):
            if extra:
                return cmd.strip(), [str(x) for x in extra]
            return _argv_from_command_string(cmd.strip())
        return _argv_from_command_string(cmd.strip())

    shell = args.get("shell") or args.get("script")
    if isinstance(shell, str) and shell.strip():
        stripped = shell.strip()
        parts = shlex.split(stripped, posix=True)
        if not parts:
            raise ValueError("shell/script is empty after parsing")
        return _argv_from_command_string(stripped)

    raise ValueError(
        "run_command needs one of: argv (non-empty list), command+args/arguments, "
        "or shell/script (parsed with shlex)"
    )



class CoreToolExecutor(ToolExecutorPort):
    """Executes built-in tools and delegates ``server__tool`` calls to :class:`MCPClient`."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        memory: MemorySubsystem | None,
        skills_path: Path,
        mcp: MCPClientPort,
        terminal: TerminalExecutor | SandboxExecutor | None = None,
        extra_tools: Sequence[CustomTool] | None = None,
        run_command_allowed_commands: list[str] | tuple[str, ...] | None = None,
        run_command_allowed_path_prefixes: list[str] | tuple[str, ...] | None = None,
        attachment_store: AttachmentStore | None = None,
        run_store: RunStore | None = None,
        scheduled_loop_store: ScheduledLoopStore | None = None,
        subagent_registry: dict[str, SubagentConfig] | None = None,
    ) -> None:
        ws_settings = workspace_settings_from_env()
        self._skills_path = Path(skills_path).resolve()
        self._workspace = WorkspaceFileService(
            Path(workspace_root).resolve(), settings=ws_settings, skills_root=self._skills_path
        )
        self._spill_read_max_lines = ws_settings.WORKSPACE_SPILL_READ_MAX_LINES
        self._spill_min_chars = spill_min_chars_from_env()
        self._memory = memory
        self._mcp = mcp
        self._attachment_store = attachment_store
        self._run_store = run_store
        self._scheduled_loop_store = scheduled_loop_store
        self._subagent_registry = dict(subagent_registry or {})
        self._terminal: TerminalExecutor | SandboxExecutor
        if terminal is not None:
            self._terminal = terminal
            self._run_cmd_allowed_commands = tuple(terminal.allowed_commands)
            self._run_cmd_allowed_paths = tuple(terminal.allowed_path_prefixes)
        else:
            cmds = (
                tuple(run_command_allowed_commands)
                if run_command_allowed_commands is not None
                else tuple(ALLOWED_COMMANDS)
            )
            paths = (
                tuple(run_command_allowed_path_prefixes)
                if run_command_allowed_path_prefixes is not None
                else tuple(ALLOWED_PATHS)
            )
            self._run_cmd_allowed_commands = cmds
            self._run_cmd_allowed_paths = paths
            _scfg = SandboxConfig.from_env()
            self._terminal = (
                SandboxExecutor(_scfg, workspace_root, skills_path=self._skills_path, allowed_commands=cmds)
                if _scfg.enabled
                else TerminalExecutor(allowed_commands=cmds, allowed_path_prefixes=paths)
            )
        self._extra_tools: dict[str, Any] = {
            ct.tool_def.name: ct for ct in (extra_tools or [])
        }

    @property
    def mcp(self) -> MCPClientPort:
        """MCP client used for built-in MCP tools and ``server__tool`` dispatch."""
        return self._mcp

    def _run_command_security_envelope(self, exc: SecurityError) -> str:
        raw = str(exc)
        if raw.startswith("Command '") and "not allowed" in raw:
            return _built_in_tool_error(
                "policy",
                raw,
                "Pick a binary from the harness allowlist (run_command); do not retry the same command name.",
                {
                    "example_argv": ["git", "--version"],
                    "allowed_commands": list(self._run_cmd_allowed_commands),
                },
            )
        if raw.startswith("Path '") and "not allowed" in raw:
            return _built_in_tool_error(
                "policy",
                raw,
                "Arguments starting with ./ or / must use an allowed prefix (see harness Runtime paths / run_command); change the path, then retry.",
                {
                    "example_argv": ["grep", "pattern", "./skills/SKILL.md"],
                    "allowed_path_prefixes": list(self._run_cmd_allowed_paths),
                },
            )
        return _built_in_tool_error(
            "policy",
            raw,
            "Adjust the shell invocation to satisfy run_command policy (see harness), then retry once.",
            {},
        )

    async def aclose(self) -> None:
        """Release resources held by the terminal executor for this session."""
        await self._terminal.aclose()

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> ToolExecutionResult:
        name = call.name
        args: dict[str, Any] = dict(call.args)
        result_text: str | None = None
        err_text: str | None = None
        handler = _tool_handler_kind(name, mcp=self._mcp, extra_tools=self._extra_tools)
        logger.debug(
            "tool dispatch %s",
            kv(
                request_id=ctx.request_id,
                thread_id=ctx.thread_id,
                tool=name,
                call_id=call.call_id,
                handler=handler,
            ),
        )

        try:
            if name == "load_file":
                return self._tool_load_file(args, ctx)
            if name == "read_file":
                result_text, err_text = self._tool_read_file(args)
            elif name == "write_file":
                result_text, err_text = self._tool_write_file(args)
            elif name == "replace_in_file":
                result_text, err_text = self._tool_replace_in_file(args)
            elif name == "glob":
                result_text, err_text = self._tool_glob(args)
            elif name == "grep":
                result_text, err_text = self._tool_grep(args)
            elif name == "apply_patch":
                result_text, err_text = self._tool_apply_patch(args)
            elif name == "search_memory":
                result_text, err_text = await self._tool_search_memory(args)
            elif name == "list_skills":
                result_text, err_text = self._tool_list_skills(ctx)
            elif name == "task":
                result_text, err_text = await self._tool_task(call, ctx)
            elif name == "run_command":
                result_text, err_text = await self._tool_run_command(args)
            elif name == "enable_mcp":
                result_text, err_text = await self._tool_enable_mcp(args)
            elif name == "disable_mcp":
                result_text, err_text = await self._tool_disable_mcp(args)
            elif name == "list_mcp_resources":
                result_text, err_text = await self._tool_list_mcp_resources(args)
            elif name == "read_mcp_resource":
                result_text, err_text = await self._tool_read_mcp_resource(args)
            elif name == "list_mcp_prompts":
                result_text, err_text = await self._tool_list_mcp_prompts(args)
            elif name == "get_mcp_prompt":
                result_text, err_text = await self._tool_get_mcp_prompt(args)
            elif name == "start_loop":
                result_text, err_text = await self._tool_start_loop(args, ctx)
            elif name == "loop_status":
                result_text, err_text = await self._tool_loop_status(args)
            elif name == "pause_loop":
                result_text, err_text = await self._tool_pause_loop(args)
            elif name == "resume_loop":
                result_text, err_text = await self._tool_resume_loop(args)
            elif name == "stop_loop":
                result_text, err_text = await self._tool_stop_loop(args)
            elif name in self._extra_tools:
                try:
                    raw = await self._extra_tools[name].execute(args)
                    if isinstance(raw, ToolExecutionResult):
                        return raw
                    result_text = raw
                    err_text = None
                except Exception as exc:
                    logger.warning(
                        "tool handler failed %s",
                        kv(
                            request_id=ctx.request_id,
                            thread_id=ctx.thread_id,
                            tool=name,
                            call_id=call.call_id,
                            handler="extra",
                        ),
                        exc_info=True,
                    )
                    result_text, err_text = None, _built_in_tool_error(
                        "runtime",
                        str(exc),
                        "Fix the underlying issue described in message, then retry once if appropriate.",
                        {"tool": name},
                    )
            else:
                mcp_pair = self._mcp.split_prefixed_tool(name)
                if mcp_pair is not None:
                    server_name, tool_name = mcp_pair
                    try:
                        text = await self._mcp.call_tool(server_name, tool_name, args)
                        result_text, err_text = text, None
                    except MCPServerNotConnectedError as exc:
                        result_text, err_text = None, str(exc)
                else:
                    result_text, err_text = None, _built_in_tool_error(
                        "runtime",
                        f"unknown tool: {name}",
                        "Use a tool from the active tool list for this turn.",
                        {"tool": name},
                    )
        except WorkspaceError as exc:
            result_text, err_text = None, _workspace_error_envelope(exc)
        except MCPDiagnosticError as exc:
            result_text, err_text = None, str(exc)
        except MCPConnectionError as exc:
            result_text, err_text = None, str(exc)
        except SecurityError as exc:
            result_text, err_text = None, self._run_command_security_envelope(exc)
        except (TimeoutError, ValueError, TypeError, OSError) as exc:
            result_text, err_text = None, _built_in_tool_error(
                "runtime",
                str(exc),
                "Fix the underlying issue described in message, then retry once if appropriate.",
                {"tool": name},
            )
        except Exception as exc:
            logger.exception("tool %s failed", name)
            result_text, err_text = None, _built_in_tool_error(
                "runtime",
                str(exc),
                "If this persists, stop retrying the same tool call and report the error.",
                {"tool": name},
            )

        if err_text is None and result_text is not None:
            skip_sanitize = skip_tool_result_sanitize(name)
            should_spill = (
                name not in _SPILL_SKIP_TOOLS
                and self._spill_min_chars > 0
                and len(result_text) >= self._spill_min_chars
            )
            if should_spill:
                result_text = _write_spill_with_inventory(
                    result_text, self._workspace.repo_root, ctx.thread_id, call.call_id
                )
                if not skip_sanitize:
                    result_text = sanitize_tool_result_text(result_text)
            else:
                if not skip_sanitize:
                    result_text = sanitize_tool_result_text(result_text)
                result_text = cap_tool_result_text(result_text)
        if err_text is not None:
            return ToolExecutionResult.err(err_text)
        return ToolExecutionResult.ok_text(result_text or "")

    def _tool_load_file(self, args: dict[str, Any], ctx: TurnContext) -> ToolExecutionResult:
        attachment_id = _str_arg(args, "attachment_id", "id")
        path = _str_arg(args, "path", "file_path", "file")
        if attachment_id and path:
            return ToolExecutionResult.err(
                "load_file accepts either attachment_id or path, not both"
            )
        if attachment_id:
            return self._load_file_from_attachment(attachment_id, ctx)
        if path:
            return self._load_file_from_path(path, ctx)
        return ToolExecutionResult.err(
            "load_file requires attachment_id or path"
        )

    @staticmethod
    def _media_result(
        mime: str, data_b64: str, meta: dict[str, object]
    ) -> ToolExecutionResult:
        if mime in IMAGE_MIME_TYPES:
            return ToolExecutionResult.ok_blocks(
                [Image(mime_type=mime, data=data_b64, metadata=meta)]
            )
        return ToolExecutionResult.ok_blocks(
            [File(mime_type=mime, data=data_b64, metadata=meta)]
        )

    def _load_file_from_attachment(
        self, attachment_id: str, ctx: TurnContext
    ) -> ToolExecutionResult:
        if self._attachment_store is None:
            return ToolExecutionResult.err("Attachments are not enabled for this session")
        if not self._attachment_store.exists(ctx.thread_id, attachment_id):
            return ToolExecutionResult.err(f"Unknown attachment_id: {attachment_id}")
        try:
            data_b64, mime, filename = self._attachment_store.read_base64(
                ctx.thread_id, attachment_id
            )
        except FileNotFoundError:
            return ToolExecutionResult.err(
                f"Attachment {attachment_id} expired or removed; ask user to re-upload"
            )
        meta: dict[str, object] = {"attachment_id": attachment_id, "filename": filename}
        return self._media_result(mime, data_b64, meta)

    def _load_file_from_path(self, path: str, ctx: TurnContext) -> ToolExecutionResult:
        try:
            fp = self._workspace._resolve_under_root(path, label="path")
        except WorkspaceError as exc:
            return ToolExecutionResult.err(_workspace_error_envelope(exc))
        if not fp.is_file():
            return ToolExecutionResult.err(f"Not a file: {path}")
        try:
            raw = fp.read_bytes()
        except OSError as exc:
            return ToolExecutionResult.err(f"Failed to read file at {path}: {exc}")

        mime = sniff_mime(raw[:512])
        if mime is None:
            ext_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".pdf": "application/pdf",
            }
            mime = ext_map.get(fp.suffix.lower())
        if mime is None or mime not in ALLOWED_MIME_TYPES:
            return ToolExecutionResult.err(
                "load_file supports images (png/jpeg/gif/webp) and PDF only; "
                f"got {mime or 'unknown'}. For text files use read_file."
            )

        max_bytes = max_image_bytes() if mime in IMAGE_MIME_TYPES else max_pdf_bytes()
        if len(raw) > max_bytes:
            return ToolExecutionResult.err(
                f"File too large ({len(raw)} bytes); max for {mime} is {max_bytes}"
            )

        data_b64 = base64.b64encode(raw).decode("ascii")
        filename = fp.name
        meta: dict[str, object] = {"filename": filename, "path": path}
        if self._attachment_store is not None:
            try:
                stored = self._attachment_store.save(
                    ctx.thread_id,
                    data=raw,
                    mime_type=mime,
                    filename=filename,
                )
                meta["attachment_id"] = stored.attachment_id
            except Exception:
                logger.warning(
                    "load_file attachment save failed %s",
                    kv(
                        request_id=ctx.request_id,
                        thread_id=ctx.thread_id,
                        path=path,
                    ),
                    exc_info=True,
                )

        return self._media_result(mime, data_b64, meta)

    def _tool_read_file(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        path = _str_arg(args, "path", "file_path", "file")
        if not path:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "read_file requires a path argument.",
                    'Pass a workspace-relative path, e.g. {"path": "README.md"}.',
                    {"field": "path", "example": {"path": "README.md"}},
                ),
            )
        offset = _coerce_int(args.get("offset"), 1) or 1
        limit = _coerce_int(args.get("limit"), None)
        max_lines_cap = None
        if _is_under_spill_path(self._workspace.repo_root, path):
            max_lines_cap = self._spill_read_max_lines
        try:
            payload = self._workspace.read_file(
                path,
                offset=offset,
                limit=limit,
                max_lines_cap=max_lines_cap,
            )
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, _workspace_error_envelope(exc))

    def _tool_write_file(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        path = _str_arg(args, "path", "file_path", "file")
        if not path:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "write_file requires a path argument.",
                    'Pass path and content, e.g. {"path": "README.md", "content": "..."}.',
                    {"field": "path", "example": {"path": "README.md", "content": ""}},
                ),
            )
        content = args.get("content")
        if content is None:
            content = args.get("body", "")
        if not isinstance(content, str):
            content = str(content)
        try:
            payload = self._workspace.write_file(path, content)
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, _workspace_error_envelope(exc))

    def _tool_replace_in_file(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        path = _str_arg(args, "path", "file_path", "file")
        if not path:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "replace_in_file requires a path argument.",
                    'Pass path, old_string, and new_string, e.g. {"path": "README.md", "old_string": "a", "new_string": "b"}.',
                    {
                        "field": "path",
                        "example": {
                            "path": "README.md",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    },
                ),
            )
        old_string = args.get("old_string")
        if old_string is None:
            old_string = args.get("old", "")
        if not isinstance(old_string, str):
            old_string = str(old_string)
        new_string = args.get("new_string")
        if new_string is None:
            new_string = args.get("new", "")
        if not isinstance(new_string, str):
            new_string = str(new_string)
        replace_all = args.get("replace_all", False)
        if not isinstance(replace_all, bool):
            replace_all = str(replace_all).strip().lower() in ("1", "true", "yes", "on")
        try:
            payload = self._workspace.replace_in_file(
                path, old_string, new_string, replace_all=replace_all
            )
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, _workspace_error_envelope(exc))

    def _tool_glob(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        pattern = _str_arg(args, "pattern", "glob")
        if not pattern:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "glob requires a pattern argument.",
                    'Pass a glob pattern, e.g. {"pattern": "**/*.html"}.',
                    {"field": "pattern", "example": {"pattern": "**/*.md"}},
                ),
            )
        root = _str_arg(args, "root")
        try:
            payload = self._workspace.glob_paths(pattern, root=root)
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, _workspace_error_envelope(exc))

    def _tool_grep(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        pattern = _str_arg(args, "pattern", "regex", "query")
        if not pattern:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "grep requires a pattern argument.",
                    'Pass a regex pattern, e.g. {"pattern": "TODO", "file_glob": "*.py"}.',
                    {"field": "pattern", "example": {"pattern": "def main", "file_glob": "*.py"}},
                ),
            )
        root = _str_arg(args, "root", "path")
        ignore_case = args.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            ignore_case = str(ignore_case).strip().lower() in ("1", "true", "yes", "on")
        file_glob = _str_arg(args, "file_glob", "include", "glob")
        max_matches = _coerce_int(args.get("max_matches"), None)
        try:
            payload = self._workspace.grep(
                pattern,
                root=root,
                ignore_case=ignore_case,
                file_glob=file_glob,
                max_matches=max_matches,
            )
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, _workspace_error_envelope(exc))

    def _tool_apply_patch(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        from monkeybot.core.tools.patch import PatchError, parse_patch, plan_and_apply_patch

        patch_text = args.get("patch_text")
        if patch_text is None:
            patch_text = args.get("patch") or args.get("text") or ""
        if not isinstance(patch_text, str):
            patch_text = str(patch_text)
        if not patch_text.strip():
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "apply_patch requires non-empty patch_text.",
                    "Pass a Codex-style patch between *** Begin Patch and *** End Patch.",
                    {"field": "patch_text"},
                ),
            )
        try:
            hunks = parse_patch(patch_text)
            payload = plan_and_apply_patch(self._workspace, hunks)
            return (_j(payload), None)
        except PatchError as exc:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    str(exc),
                    "Fix the patch (markers, paths, or hunk context) and retry once.",
                    {"code": getattr(exc, "code", "patch_error")},
                ),
            )
        except WorkspaceError as exc:
            return (None, _workspace_error_envelope(exc))

    async def _tool_search_memory(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        query = _str_arg(args, "query", "q", "keyword", "phrase")
        if not query:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "search_memory requires a non-empty query.",
                    'Use query (or q / keyword / phrase), e.g. {"query": "deployment"}.',
                    {"field": "query", "example": {"query": "keyword"}},
                ),
            )
        max_hits = _coerce_int(args.get("max_hits"), 40) or 40
        if self._memory is None:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "search_memory requires memory to be configured.",
                    "Enable the memory hook and set paths.memory_storage_uri in monkeybot.yaml.",
                    {"field": "memory"},
                ),
            )
        payload = await self._memory.search_files(query, max_hits=max_hits, skip_raw=False)
        return (_j(payload), None)

    def _tool_list_skills(self, ctx: TurnContext) -> tuple[str | None, str | None]:
        rows = [
            {"name": s.name, "description": s.description}
            for s in ctx.skills
        ]
        return (
            _j(
                {
                    "ok": True,
                    "skills_path": str(self._skills_path),
                    "skills": rows,
                }
            ),
            None,
        )

    async def _tool_task(self, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
        args = dict(call.args)
        task = _str_arg(args, "task", "instructions", "prompt", "objective")
        if not task:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "task requires a non-empty string in the task (or instructions / prompt / objective) field.",
                    'Example: {"task": "Summarize the open issue and propose a fix."}.',
                    {"field": "task", "example": {"task": "Do the thing"}},
                ),
            )

        context_val = args.get("context") or args.get("background") or ""
        if not isinstance(context_val, str):
            context_val = str(context_val)

        subagent_type = _str_arg(args, "subagent_type", "type", "persona")
        agent_root = resolve_agent_project_root()
        try:
            agent_md_path = resolve_task_agent_md_path(
                subagent_type=subagent_type,
                registry=self._subagent_registry,
                agent_root=agent_root,
            )
        except ValueError as exc:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    str(exc),
                    "Use a subagent_type from the harness Subagent personas list, or omit it for the default.",
                    {"field": "subagent_type", "subagent_type": subagent_type},
                ),
            )

        memory_uri = self._memory.uri if self._memory is not None else ""

        script = resolve_subagent_script()
        if not script.is_file():
            return (
                None,
                _built_in_tool_error(
                    "runtime",
                    f"task: worker script missing at {script}",
                    "Set MONKEYBOT_SUBAGENT_SCRIPT to a valid subagent_worker.py path.",
                    {"path": str(script)},
                ),
            )

        parent_label = f"{ctx.request_id}:{call.call_id}"
        traceparent = _inject_subagent_traceparent()
        envelope = SubagentEnvelope(
            task=task,
            context=context_val,
            memory_storage_uri=memory_uri,
            parent_run_id=parent_label,
            model=ctx.model,
            traceparent=traceparent,
            agent_md=str(agent_md_path),
            subagent_type=subagent_type,
        )

        scratch = (self._workspace.repo_root / ".monkeybot" / "subagent-runs" / uuid.uuid4().hex)
        scratch.mkdir(parents=True, exist_ok=True)

        run_id = make_run_id()
        persisted = PersistedSubagentEnvelope(
            task=envelope.task,
            context=envelope.context,
            memory_storage_uri=envelope.memory_storage_uri,
            parent_run_id=envelope.parent_run_id,
            model=envelope.model,
            traceparent=envelope.traceparent,
            agent_md=envelope.agent_md,
            subagent_type=envelope.subagent_type,
        )
        queue_mode = os.environ.get("MONKEYBOT_TASK_QUEUE", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if queue_mode and self._run_store is None:
            raise RuntimeError("MONKEYBOT_TASK_QUEUE=1 requires a configured storage backend")
        if self._run_store is not None:
            if queue_mode:
                await self._run_store.record_pending(
                    run_id=run_id,
                    parent_run_id=parent_label,
                    script=str(script),
                    envelope=persisted,
                    scratch_dir=scratch,
                )
                return (
                    _j(
                        {
                            "ok": True,
                            "queued": True,
                            "run_id": run_id,
                            "scratch_dir": str(scratch),
                            "message": "Subagent run queued for worker pool.",
                        }
                    ),
                    None,
                )
            await self._run_store.record_started(
                run_id=run_id,
                parent_run_id=parent_label,
                script=str(script),
                envelope=persisted,
                scratch_dir=scratch,
            )

        child_env = {
            "MONKEYBOT_SUBAGENT_WORKSPACE": str(self._workspace.repo_root),
            "MONKEYBOT_AGENT_ROOT": str(agent_root),
            "MEMORY_STORAGE_URI": memory_uri,
            "MONKEYBOT_SUBAGENT_SKILLS_PATH": str(self._skills_path),
            "OTEL_SERVICE_NAME": _SUBAGENT_OTEL_SERVICE_NAME,
            "MONKEYBOT_SUBAGENT_AGENT_MD": str(agent_md_path),
        }

        for env_key, raw_val in (
            ("MCP_CONFIG", os.environ.get("MCP_CONFIG", "")),
            ("COMMAND_ALLOWLIST_CONFIG", os.environ.get("COMMAND_ALLOWLIST_CONFIG", "")),
        ):
            if raw_val.strip():
                child_env[env_key] = str(resolve_project_path(raw_val.strip(), agent_root))

        db_raw = os.environ.get("DB_URL", "").strip()
        if db_raw:
            child_env["DB_URL"] = normalize_sqlite_db_url(db_raw, agent_root)

        timeout_raw = os.environ.get("SUBAGENT_TIMEOUT_SEC", "600").strip()
        try:
            timeout = max(1.0, float(timeout_raw))
        except ValueError:
            timeout = 600.0

        deltas: list[str] = []
        errors: list[str] = []
        tool_call_count = 0
        tool_results: list[dict[str, str]] = []
        turn_complete: TurnComplete | None = None
        proc_holder: list[asyncio.subprocess.Process | None] = [None]

        async def _subprocess_exec(*cmd: str | bytes) -> asyncio.subprocess.Process:
            env = dict(os.environ)
            env.update(child_env)
            env["PYTHONUNBUFFERED"] = "1"
            p = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            proc_holder[0] = p
            return p

        async def _drain() -> None:
            nonlocal turn_complete, tool_call_count
            async for evt in spawn_subagent(
                str(script),
                envelope,
                scratch_dir=scratch,
                subprocess_exec=_subprocess_exec,
            ):
                if isinstance(evt, AssistantDelta):
                    deltas.append(evt.delta)
                elif isinstance(evt, ToolCallStarted):
                    tool_call_count += 1
                elif isinstance(evt, ToolCallResult):
                    snippet = (evt.result or evt.error or "").strip()
                    if len(snippet) > 600:
                        snippet = snippet[:600] + "…"
                    tool_results.append({"tool": evt.tool, "snippet": snippet})
                elif isinstance(evt, Error):
                    errors.append(evt.error)
                elif isinstance(evt, TurnComplete):
                    turn_complete = evt

        drain_task = asyncio.create_task(_drain())
        cancel_wait = asyncio.create_task(ctx.cancelled.wait()) if ctx.cancelled is not None else None

        try:
            if cancel_wait is None:
                try:
                    await asyncio.wait_for(drain_task, timeout=timeout)
                except TimeoutError:
                    errors.append(f"task: subagent exceeded {timeout:g}s timeout")
                    await _stop_subagent_process(proc_holder[0])
                    if not drain_task.done():
                        drain_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await drain_task
            else:
                done, _ = await asyncio.wait(
                    {drain_task, cancel_wait},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    errors.append(f"task: subagent exceeded {timeout:g}s timeout")
                    await _stop_subagent_process(proc_holder[0])
                    cancel_wait.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_wait
                    if not drain_task.done():
                        drain_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await drain_task
                elif drain_task in done:
                    if not cancel_wait.done():
                        cancel_wait.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await cancel_wait
                    try:
                        drain_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        errors.append(str(exc))
                else:
                    errors.append(_PARENT_CANCEL_TASK_ERR)
                    await _stop_subagent_process(proc_holder[0])
                    if not drain_task.done():
                        drain_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await drain_task
        finally:
            if cancel_wait is not None and not cancel_wait.done():
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait

        usage_payload = None
        if turn_complete is not None:
            u = turn_complete.usage
            usage_payload = {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cached_tokens": u.cached_tokens,
                "cost_usd": u.cost_usd,
                "duration_ms": u.duration_ms,
            }

        full_text = "".join(deltas).strip()
        final_text = full_text

        payload = {
            "ok": len(errors) == 0,
            "final_message": final_text,
            "assistant_text": full_text,
            "tool_call_count": tool_call_count,
            "tool_results": tool_results[-10:],
            "errors": errors,
            "usage": usage_payload,
            "scratch_dir": str(scratch),
            "run_id": run_id,
        }
        if self._run_store is not None and not queue_mode:
            result_json = _j(payload)
            if len(errors) == 0:
                await self._run_store.record_completed(run_id, result_json)
            else:
                await self._run_store.record_failed(run_id, "; ".join(errors))
        return (_j(payload), None)

    async def _tool_run_command(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        try:
            cmd, argv = _parse_run_command(args)
        except ValueError as exc:
            return None, _run_command_parse_envelope(exc)
        timeout = _coerce_int(args.get("timeout"), 60) or 60
        try:
            result = await self._terminal.execute(
                cmd,
                argv,
                timeout=timeout,
                cwd=self._workspace.repo_root,
            )
        except SecurityError as exc:
            return None, self._run_command_security_envelope(exc)
        except TimeoutError as exc:
            return (
                None,
                _built_in_tool_error(
                    "runtime",
                    str(exc),
                    "Increase run_command timeout (seconds) or use a shorter command, then retry once.",
                    {"example": {"argv": ["git", "--version"], "timeout": 120}},
                ),
            )
        return (
            _j(
                {
                    "ok": result.exit_code == 0,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                }
            ),
            None,
        )

    async def _tool_enable_mcp(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "name")
        if not sname:
            return (None, "enable_mcp requires name")
        already = self._mcp.is_connected(sname)
        try:
            defs = await self._mcp.connect_from_catalog(sname)
        except MCPDiagnosticError as exc:
            logger.warning("enable_mcp failed %s", kv(server=sname, error=str(exc)))
            return (None, str(exc))
        except MCPConnectionError as exc:
            logger.warning("enable_mcp failed %s", kv(server=sname, error=str(exc)))
            return (None, str(exc))
        status_snap = self._mcp.status(sname)
        logger.info(
            "enable_mcp ok %s",
            kv(server=sname, already_connected=already, tools=len(defs)),
        )
        return (
            _j(
                {
                    "ok": True,
                    "server": sname,
                    "status": status_snap,
                    "already_connected": already,
                    "tools": [{"name": t.name, "description": t.description} for t in defs],
                    "note": "New tools apply on the next model step this turn.",
                }
            ),
            None,
        )

    async def _tool_disable_mcp(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "name")
        if not sname:
            return (None, "disable_mcp requires name")
        known = list(self._mcp.known_server_names())
        if sname not in known and not self._mcp.is_connected(sname):
            known_msg = ", ".join(known) if known else "(none)"
            return (
                None,
                f"Unknown MCP server {sname!r}. Known servers: {known_msg}",
            )
        await self._mcp.disconnect(sname)
        logger.info("disable_mcp ok %s", kv(server=sname, disconnected=True))
        return (
            _j(
                {
                    "ok": True,
                    "server": sname,
                    "disconnected": True,
                    "note": "Tools drop on the next model step this turn.",
                }
            ),
            None,
        )

    async def _mcp_meta_call(
        self,
        tool_name: str,
        coro: Awaitable[Any],
        *,
        ok_payload: Callable[[Any], dict[str, Any]] | None = None,
    ) -> tuple[str | None, str | None]:
        """Run an MCP meta-tool coroutine; log and surface typed failures like enable_mcp."""
        try:
            result = await coro
        except MCPServerNotConnectedError as exc:
            logger.warning("%s failed %s", tool_name, kv(error=str(exc)))
            return (None, str(exc))
        except MCPDiagnosticError as exc:
            logger.warning(
                "%s failed %s",
                tool_name,
                kv(server=exc.server_name, error=str(exc)),
            )
            return (None, str(exc))
        except Exception as exc:
            logger.warning("%s failed %s", tool_name, kv(error=str(exc)))
            return (None, f"{tool_name} failed: {exc}")
        if ok_payload is None:
            return (_j({"ok": True, **result}), None)
        return (_j({"ok": True, **ok_payload(result)}), None)

    async def _tool_list_mcp_resources(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "server") or None
        return await self._mcp_meta_call(
            "list_mcp_resources",
            self._mcp.list_resources(sname),
            ok_payload=lambda resources: {"resources": resources, "count": len(resources)},
        )

    async def _tool_read_mcp_resource(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "server")
        uri = _str_arg(args, "uri")
        if not sname:
            return (None, "read_mcp_resource requires server")
        if not uri:
            return (None, "read_mcp_resource requires uri")
        return await self._mcp_meta_call(
            "read_mcp_resource",
            self._mcp.read_resource(sname, uri),
        )

    async def _tool_list_mcp_prompts(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "server") or None
        return await self._mcp_meta_call(
            "list_mcp_prompts",
            self._mcp.list_prompts(sname),
            ok_payload=lambda prompts: {"prompts": prompts, "count": len(prompts)},
        )

    async def _tool_get_mcp_prompt(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "server")
        prompt_name = _str_arg(args, "prompt")
        if not sname:
            return (None, "get_mcp_prompt requires server")
        if not prompt_name:
            return (None, "get_mcp_prompt requires prompt")
        raw_args = args.get("arguments")
        prompt_args: dict[str, str] = {}
        if isinstance(raw_args, dict):
            prompt_args = {str(k): "" if v is None else str(v) for k, v in raw_args.items()}
        return await self._mcp_meta_call(
            "get_mcp_prompt",
            self._mcp.get_prompt(sname, prompt_name, prompt_args or None),
        )

    def _require_loop_store(self) -> ScheduledLoopStore | tuple[None, str]:
        if self._scheduled_loop_store is None:
            return (
                None,
                _built_in_tool_error(
                    "policy",
                    "scheduled loops require durable storage (DB_URL: sqlite, postgresql, or firestore)",
                    "Configure paths.db_url in monkeybot.yaml and enable MONKEYBOT_SCHEDULER_ENABLED=1 on the gateway.",
                    {},
                ),
            )
        return self._scheduled_loop_store

    async def _tool_start_loop(
        self, args: dict[str, Any], ctx: TurnContext
    ) -> tuple[str | None, str | None]:
        store_or_err = self._require_loop_store()
        if isinstance(store_or_err, tuple):
            return None, store_or_err[1]
        store = store_or_err
        prompt = _str_arg(args, "prompt", "plan", "message")
        if not prompt:
            return (None, "start_loop requires prompt with the agreed loop plan")
        interval_raw = args.get("interval")
        if interval_raw is None:
            return (None, "start_loop requires interval (e.g. 20s, 5m, 1h)")
        try:
            interval_ms = parse_interval_ms(interval_raw)
            max_runtime_ms = parse_optional_duration_ms(args.get("max_runtime"))
        except ValueError as exc:
            return (None, str(exc))
        max_ticks_raw = args.get("max_ticks")
        max_ticks: int | None = None
        if max_ticks_raw is not None:
            max_ticks = _coerce_int(max_ticks_raw, 0)
            if max_ticks is None or max_ticks < 1:
                return (None, "max_ticks must be a positive integer when set")
        session_id = _str_arg(args, "session_id") or ctx.thread_id
        loop_id = _str_arg(args, "loop_id")
        skip_if_busy = args.get("skip_if_busy", True)
        if not isinstance(skip_if_busy, bool):
            skip_if_busy = True
        unbounded = args.get("unbounded", False)
        if not isinstance(unbounded, bool):
            unbounded = False
        spec = ScheduledLoopCreate(
            prompt=prompt,
            interval_ms=interval_ms,
            session_id=session_id,
            loop_id=loop_id,
            max_ticks=max_ticks,
            max_runtime_ms=max_runtime_ms,
            skip_if_busy=skip_if_busy,
            unbounded=unbounded,
        )
        try:
            row = await store.create(spec)
        except ValueError as exc:
            return (None, str(exc))
        return (
            _j(
                {
                    "ok": True,
                    "loop_id": row.loop_id,
                    "session_id": row.session_id,
                    "status": row.status,
                    "interval_ms": row.interval_ms,
                    "max_ticks": row.max_ticks,
                    "max_runtime_ms": row.max_runtime_ms,
                    "message": (
                        "Scheduled loop registered. The scheduler worker will fire the prompt "
                        "on each tick when MONKEYBOT_SCHEDULER_ENABLED=1 or the standalone "
                        "scheduler worker is running."
                    ),
                }
            ),
            None,
        )

    async def _tool_loop_status(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        store_or_err = self._require_loop_store()
        if isinstance(store_or_err, tuple):
            return None, store_or_err[1]
        store = store_or_err
        loop_id = _str_arg(args, "loop_id", "id")
        if loop_id:
            row = await store.get(loop_id)
            if row is None:
                return (None, f"unknown loop: {loop_id}")
            return (_j({"ok": True, "loop": self._loop_row_json(row)}), None)
        rows = await store.list_all()
        return (_j({"ok": True, "loops": [self._loop_row_json(r) for r in rows]}), None)

    @staticmethod
    def _loop_row_json(row: object) -> dict[str, object]:
        from monkeybot.core.persistence.scheduled_loops import ScheduledLoopRow

        if not isinstance(row, ScheduledLoopRow):
            raise TypeError("expected ScheduledLoopRow")
        return {
            "loop_id": row.loop_id,
            "session_id": row.session_id,
            "status": row.status,
            "tick_index": row.tick_index,
            "interval_ms": row.interval_ms,
            "max_ticks": row.max_ticks,
            "max_runtime_ms": row.max_runtime_ms,
            "next_tick_at_ms": row.next_tick_at_ms,
            "last_tick_at_ms": row.last_tick_at_ms,
            "last_error": row.last_error,
            "stop_reason": row.stop_reason,
            "tick_in_flight": row.tick_in_flight,
        }

    async def _tool_pause_loop(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        store_or_err = self._require_loop_store()
        if isinstance(store_or_err, tuple):
            return None, store_or_err[1]
        loop_id = _str_arg(args, "loop_id", "id")
        if not loop_id:
            return (None, "pause_loop requires loop_id")
        ok = await store_or_err.pause(loop_id)
        if not ok:
            return (None, f"unknown loop: {loop_id}")
        return (_j({"ok": True, "loop_id": loop_id, "status": "paused"}), None)

    async def _tool_resume_loop(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        store_or_err = self._require_loop_store()
        if isinstance(store_or_err, tuple):
            return None, store_or_err[1]
        loop_id = _str_arg(args, "loop_id", "id")
        if not loop_id:
            return (None, "resume_loop requires loop_id")
        ok = await store_or_err.resume(loop_id)
        if not ok:
            return (None, f"paused loop not found: {loop_id}")
        return (_j({"ok": True, "loop_id": loop_id, "status": "active"}), None)

    async def _tool_stop_loop(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        store_or_err = self._require_loop_store()
        if isinstance(store_or_err, tuple):
            return None, store_or_err[1]
        loop_id = _str_arg(args, "loop_id", "id")
        if not loop_id:
            return (None, "stop_loop requires loop_id")
        ok = await store_or_err.stop(loop_id)
        if not ok:
            return (None, f"unknown loop: {loop_id}")
        return (_j({"ok": True, "loop_id": loop_id, "status": "completed"}), None)
