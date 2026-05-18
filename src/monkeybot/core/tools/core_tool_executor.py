"""Default tool executor: core filesystem, memory search, shell (allowlisted), and MCP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import uuid
from pathlib import Path
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.mcp.mcp_client import MCPConnectionError, MCPServerNotConnectedError
from monkeybot.core.mcp.ports_mcp import MCPClientPort
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.runtime.events import (
    AssistantDelta,
    Error,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
)
from monkeybot.core.runtime.loop import ToolExecutorPort
from monkeybot.core.subagents.subagent_proto import SubagentEnvelope, spawn_subagent
from monkeybot.core.tools.terminal import (
    ALLOWED_COMMANDS,
    ALLOWED_PATHS,
    SecurityError,
    TerminalExecutor,
)
from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService

logger = logging.getLogger(__name__)

_SUBAGENT_OTEL_SERVICE_NAME = "monkeybot-subagent"

_PARENT_CANCEL_TASK_ERR = "task: cancelled (parent)"

_SPILL_DIR = ".monkeybot/spill"
_SPILL_MAX_CHARS = 20_000
_SPILL_READ_LIMIT = 500


def _safe_spill_filename(call_id: str) -> str:
    safe = "".join(c for c in call_id if c.isalnum() or c in "-_")[:200]
    return safe or "call"


def _write_spill_and_cap(
    text: str,
    workspace_root: Path,
    thread_id: str,
    call_id: str,
) -> str:
    """Write full ``text`` to spill file; return capped body with path hint."""
    rel = f"{_SPILL_DIR}/{thread_id}/{_safe_spill_filename(call_id)}.txt"
    out_path = (Path(workspace_root) / rel).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    prefix = text[:_SPILL_MAX_CHARS]
    note = (
        f"\n[Result truncated — {len(text)} total chars. Full output at: {rel} — "
        "use read_file with offset/limit to page through it.]"
    )
    return prefix + note


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
    elif code == "invalid_offset":
        hint = 'Use "offset" as a positive integer (1 = first line).'
    elif code in ("write_failed", "glob_failed"):
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
            return cmd.strip(), [str(x) for x in extra]
        parts = shlex.split(cmd, posix=True)
        if parts:
            return parts[0], parts[1:]

    shell = args.get("shell") or args.get("script")
    if isinstance(shell, str) and shell.strip():
        parts = shlex.split(shell.strip(), posix=True)
        if not parts:
            raise ValueError("shell/script is empty after parsing")
        return parts[0], parts[1:]

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
        terminal: TerminalExecutor | None = None,
        extra_tools: list | None = None,
        run_command_allowed_commands: list[str] | tuple[str, ...] | None = None,
        run_command_allowed_path_prefixes: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._workspace = WorkspaceFileService(Path(workspace_root).resolve())
        self._memory = memory
        self._skills_path = Path(skills_path).resolve()
        self._mcp = mcp
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
            from monkeybot.core.tools.sandbox_executor import SandboxConfig, SandboxExecutor

            _scfg = SandboxConfig.from_env()
            self._terminal = (
                SandboxExecutor(_scfg, workspace_root, allowed_commands=cmds)
                if _scfg.enabled
                else TerminalExecutor(allowed_commands=cmds, allowed_path_prefixes=paths)
            )
        self._extra_tools: dict[str, Any] = {
            ct.tool_def.name: ct for ct in (extra_tools or [])
        }

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

    async def execute(self, *, call: ToolCall, ctx: TurnContext) -> tuple[str | None, str | None]:
        name = call.name
        args: dict[str, Any] = dict(call.args)
        result_text: str | None = None
        err_text: str | None = None

        try:
            if name == "read_file":
                result_text, err_text = self._tool_read_file(args)
            elif name == "write_file":
                result_text, err_text = self._tool_write_file(args)
            elif name == "search_memory":
                result_text, err_text = await self._tool_search_memory(args)
            elif name == "list_skills":
                result_text, err_text = self._tool_list_skills(ctx)
            elif name == "task":
                result_text, err_text = await self._tool_task(call, ctx)
            elif name == "run_command":
                result_text, err_text = await self._tool_run_command(args)
            elif name == "add_mcp_server":
                result_text, err_text = await self._tool_add_mcp_server(args)
            elif name == "remove_mcp_server":
                result_text, err_text = await self._tool_remove_mcp_server(args)
            elif name in self._extra_tools:
                try:
                    result_text = await self._extra_tools[name].execute(args)
                    err_text = None
                except Exception as exc:
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

        if err_text is None and result_text is not None and len(result_text) > _SPILL_MAX_CHARS:
            result_text = _write_spill_and_cap(
                result_text, self._workspace.repo_root, ctx.thread_id, call.call_id
            )
        return result_text, err_text

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
        if _is_under_spill_path(self._workspace.repo_root, path):
            limit = min(limit or _SPILL_READ_LIMIT, _SPILL_READ_LIMIT)
        try:
            payload = self._workspace.read_file(path, offset=offset, limit=limit)
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

        if self._memory is None:
            return (
                None,
                _built_in_tool_error(
                    "validation",
                    "task tool requires memory to be configured for the subagent worker.",
                    "Enable the memory hook and set paths.memory_storage_uri in monkeybot.yaml.",
                    {"field": "memory"},
                ),
            )

        script = Path(
            os.environ.get(
                "MONKEYBOT_SUBAGENT_SCRIPT",
                str(Path(__file__).resolve().parent / "subagent_worker.py"),
            )
        ).resolve()
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
            memory_storage_uri=self._memory.uri,
            parent_run_id=parent_label,
            model=ctx.model,
            traceparent=traceparent,
        )

        scratch = (self._workspace.repo_root / ".monkeybot" / "subagent-runs" / uuid.uuid4().hex)
        scratch.mkdir(parents=True, exist_ok=True)

        child_env = {
            "MONKEYBOT_SUBAGENT_WORKSPACE": str(self._workspace.repo_root),
            "MEMORY_STORAGE_URI": self._memory.uri,
            "MONKEYBOT_SUBAGENT_SKILLS_PATH": str(self._skills_path),
            "OTEL_SERVICE_NAME": _SUBAGENT_OTEL_SERVICE_NAME,
        }
        agent_md = os.environ.get("AGENT_MD")
        if agent_md:
            child_env["MONKEYBOT_SUBAGENT_AGENT_MD"] = agent_md

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
        }
        return (_j(payload), None)

    async def _tool_run_command(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        try:
            cmd, argv = _parse_run_command(args)
        except ValueError as exc:
            return None, _run_command_parse_envelope(exc)
        timeout = _coerce_int(args.get("timeout"), 60) or 60
        try:
            result = await self._terminal.execute(cmd, argv, timeout=timeout)
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

    async def _tool_add_mcp_server(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "name", "server_name", "server")
        if not sname:
            return (None, "add_mcp_server requires name (or server_name / server)")
        command = _str_arg(args, "command", "cmd")
        if not command:
            return (None, "add_mcp_server requires command")
        raw_args = args.get("args")
        arg_list = [str(x) for x in raw_args] if isinstance(raw_args, list) else []
        env: dict[str, str] = {}
        env_src = args.get("env")
        if isinstance(env_src, dict):
            for k, val in env_src.items():
                env[str(k)] = "" if val is None else str(val)
        defs = await self._mcp.connect(sname, command, arg_list, env)
        return (
            _j(
                {
                    "ok": True,
                    "server": sname,
                    "tools": [{"name": t.name, "description": t.description} for t in defs],
                    "note": "New tools apply on the next user message (context is built per turn).",
                }
            ),
            None,
        )

    async def _tool_remove_mcp_server(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        sname = _str_arg(args, "name", "server_name", "server")
        if not sname:
            return (None, "remove_mcp_server requires name (or server_name / server)")
        await self._mcp.disconnect(sname)
        return (_j({"ok": True, "server": sname, "disconnected": True}), None)
