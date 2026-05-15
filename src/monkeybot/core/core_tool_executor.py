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
from monkeybot.core.memory import search_memory_files
from monkeybot.core.events import AssistantDelta, Error, ToolCallResult, ToolCallStarted, TurnComplete
from monkeybot.core.loop import ToolExecutorPort
from monkeybot.core.mcp_client import MCPConnectionError, MCPServerNotConnectedError
from monkeybot.core.ports_mcp import MCPClientPort
from monkeybot.core.provider import ToolCall
from monkeybot.core.subagent_proto import SubagentEnvelope, spawn_subagent
from monkeybot.core.terminal import SecurityError, TerminalExecutor
from monkeybot.core.workspace_service import WorkspaceError, WorkspaceFileService

logger = logging.getLogger(__name__)

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
        memory_path: Path,
        skills_path: Path,
        mcp: MCPClientPort,
        terminal: TerminalExecutor | None = None,
    ) -> None:
        self._workspace = WorkspaceFileService(Path(workspace_root).resolve())
        self._memory_path = Path(memory_path).resolve()
        self._skills_path = Path(skills_path).resolve()
        self._mcp = mcp
        self._terminal = terminal if terminal is not None else TerminalExecutor()

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
                    result_text, err_text = None, f"unknown tool: {name}"
        except WorkspaceError as exc:
            result_text, err_text = None, str(exc)
        except MCPConnectionError as exc:
            result_text, err_text = None, str(exc)
        except (SecurityError, TimeoutError, ValueError, TypeError, OSError) as exc:
            result_text, err_text = None, str(exc)
        except Exception as exc:
            logger.exception("tool %s failed", name)
            result_text, err_text = None, str(exc)

        if err_text is None and result_text is not None and len(result_text) > _SPILL_MAX_CHARS:
            result_text = _write_spill_and_cap(
                result_text, self._workspace.repo_root, ctx.thread_id, call.call_id
            )
        return result_text, err_text

    def _tool_read_file(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        path = _str_arg(args, "path", "file_path", "file")
        if not path:
            return (None, "read_file requires path")
        offset = _coerce_int(args.get("offset"), 1) or 1
        limit = _coerce_int(args.get("limit"), None)
        if _is_under_spill_path(self._workspace.repo_root, path):
            limit = min(limit or _SPILL_READ_LIMIT, _SPILL_READ_LIMIT)
        try:
            payload = self._workspace.read_file(path, offset=offset, limit=limit)
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, str(exc))

    def _tool_write_file(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        path = _str_arg(args, "path", "file_path", "file")
        if not path:
            return (None, "write_file requires path")
        content = args.get("content")
        if content is None:
            content = args.get("body", "")
        if not isinstance(content, str):
            content = str(content)
        try:
            payload = self._workspace.write_file(path, content)
            return (_j(payload), None)
        except WorkspaceError as exc:
            return (None, str(exc))

    async def _tool_search_memory(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        query = _str_arg(args, "query", "q", "keyword", "phrase")
        if not query:
            return (None, "search_memory requires query (or q / keyword / phrase)")
        max_hits = _coerce_int(args.get("max_hits"), 40) or 40
        payload = await asyncio.to_thread(search_memory_files, self._memory_path, query, max_hits=max_hits)
        return (_j(payload), None)

    def _tool_list_skills(self, ctx: TurnContext) -> tuple[str | None, str | None]:
        rows = [
            {"name": s.name, "description": s.description, "entry_point": s.entry_point}
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
            return None, "task requires a non-empty string argument 'task'"

        context_val = args.get("context") or args.get("background") or ""
        if not isinstance(context_val, str):
            context_val = str(context_val)

        script = Path(
            os.environ.get(
                "MONKEYBOT_SUBAGENT_SCRIPT",
                str(Path(__file__).resolve().parent / "subagent_worker.py"),
            )
        ).resolve()
        if not script.is_file():
            return None, f"task: worker script missing at {script}"

        parent_label = f"{ctx.request_id}:{call.call_id}"
        envelope = SubagentEnvelope(
            task=task,
            context=context_val,
            memory_path=str(self._memory_path),
            parent_run_id=parent_label,
            model=ctx.model,
        )

        scratch = (self._workspace.repo_root / ".monkeybot" / "subagent-runs" / uuid.uuid4().hex)
        scratch.mkdir(parents=True, exist_ok=True)

        child_env = {
            "MONKEYBOT_SUBAGENT_WORKSPACE": str(self._workspace.repo_root),
            "MONKEYBOT_SUBAGENT_MEMORY_PATH": str(self._memory_path),
            "MONKEYBOT_SUBAGENT_SKILLS_PATH": str(self._skills_path),
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
                except asyncio.TimeoutError:
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
        cmd, argv = _parse_run_command(args)
        timeout = _coerce_int(args.get("timeout"), 60) or 60
        result = await self._terminal.execute(cmd, argv, timeout=timeout)
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
