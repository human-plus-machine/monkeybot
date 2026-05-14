"""Default tool executor: core filesystem, memory search, shell (allowlisted), and MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import uuid
from pathlib import Path
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.events import AssistantDelta, Error, ToolCallResult, ToolCallStarted, TurnComplete
from monkeybot.core.loop import ToolExecutorPort
from monkeybot.core.mcp_client import MCPConnectionError, MCPServerNotConnectedError
from monkeybot.core.ports_mcp import MCPClientPort
from monkeybot.core.provider import ToolCall
from monkeybot.core.subagent_proto import SubagentEnvelope, spawn_subagent
from monkeybot.core.terminal import SecurityError, TerminalExecutor
from monkeybot.core.workspace_service import WorkspaceError, WorkspaceFileService

logger = logging.getLogger(__name__)


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


def _last_clean_assistant_text(text: str) -> str:
    """Return the model's natural-language reply, stripping ``{"tool_calls":...}`` placeholder echoes.

    The owned loop stores assistant turns that requested tools as ``"<text>\\n<json>"`` rows; some
    models then imitate that shape in their text stream. Drop trailing JSON-only segments so the
    parent surfaces the real prose instead of an internal-looking blob.
    """
    body = (text or "").strip()
    if not body:
        return ""
    last_nl = body.rfind("\n")
    if last_nl == -1:
        candidate = body
        rest = ""
    else:
        candidate = body[last_nl + 1 :].strip()
        rest = body[:last_nl].rstrip()
    if candidate.startswith("{") and '"tool_calls"' in candidate:
        try:
            json.loads(candidate)
            return rest
        except json.JSONDecodeError:
            return body
    return body


def _search_memory_disk(memory_root: Path, query: str, *, max_hits: int = 40) -> dict[str, Any]:
    q = query.lower().strip()
    if not q:
        return {"ok": True, "query": query, "hits": [], "note": "empty query"}
    if not memory_root.exists():
        return {"ok": True, "query": query, "hits": [], "note": f"missing directory: {memory_root}"}

    hits: list[dict[str, Any]] = []
    suffixes = {".md", ".txt", ".markdown"}
    for path in sorted(memory_root.rglob("*")):
        if len(hits) >= max_hits:
            break
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        pos = lower.find(q)
        if pos < 0:
            continue
        try:
            rel = str(path.relative_to(memory_root))
        except ValueError:
            rel = path.name
        start = max(0, pos - 60)
        end = min(len(text), pos + len(q) + 80)
        snippet = text[start:end].replace("\n", " ")
        hits.append({"path": rel, "snippet": snippet, "match_offset": pos})
    return {"ok": True, "query": query, "hits": hits, "truncated": len(hits) >= max_hits}


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

        try:
            if name == "read_file":
                return self._tool_read_file(args)
            if name == "write_file":
                return self._tool_write_file(args)
            if name == "search_memory":
                return await self._tool_search_memory(args)
            if name == "list_skills":
                return self._tool_list_skills(ctx)
            if name == "task":
                return await self._tool_task(call, ctx)
            if name == "run_command":
                return await self._tool_run_command(args)
            if name == "add_mcp_server":
                return await self._tool_add_mcp_server(args)
            if name == "remove_mcp_server":
                return await self._tool_remove_mcp_server(args)

            mcp_pair = self._mcp.split_prefixed_tool(name)
            if mcp_pair is not None:
                server_name, tool_name = mcp_pair
                try:
                    text = await self._mcp.call_tool(server_name, tool_name, args)
                    return (text, None)
                except MCPServerNotConnectedError as exc:
                    return (None, str(exc))

            return (None, f"unknown tool: {name}")
        except WorkspaceError as exc:
            return (None, str(exc))
        except MCPConnectionError as exc:
            return (None, str(exc))
        except (SecurityError, TimeoutError, ValueError, TypeError, OSError) as exc:
            return (None, str(exc))
        except Exception as exc:
            logger.exception("tool %s failed", name)
            return (None, str(exc))

    def _tool_read_file(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        path = _str_arg(args, "path", "file_path", "file")
        if not path:
            return (None, "read_file requires path")
        offset = _coerce_int(args.get("offset"), 1) or 1
        limit = _coerce_int(args.get("limit"), None)
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
        payload = await asyncio.to_thread(_search_memory_disk, self._memory_path, query, max_hits=max_hits)
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

        async def _subprocess_exec(*cmd: str | bytes) -> asyncio.subprocess.Process:
            env = dict(os.environ)
            env.update(child_env)
            env["PYTHONUNBUFFERED"] = "1"
            return await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )

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

        try:
            await asyncio.wait_for(_drain(), timeout=timeout)
        except asyncio.TimeoutError:
            errors.append(f"task: subagent exceeded {timeout:g}s timeout")

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
        final_text = _last_clean_assistant_text(full_text)

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
