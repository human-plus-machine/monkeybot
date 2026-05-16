"""MCP client using the official ``mcp`` Python SDK (stdio and Streamable HTTP).

``mcp`` (and ``httpx`` for remote URLs) are imported only inside connection helpers so
``python -c "import monkeybot.core.mcp.mcp_client"`` succeeds without those packages installed.

**call_tool result text:** Returned strings concatenate ``text`` from each content block
whose ``type`` is ``text``. Any other block is JSON-serialized via ``model_dump``
(``by_alias=True``) when present, otherwise :func:`str`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from monkeybot.core.types.types_tools import ToolDef

logger = logging.getLogger(__name__)


def _exception_group_leaves(exc: BaseException) -> list[BaseException]:
    """Flatten nested :class:`BaseExceptionGroup` leaves (Python 3.11+)."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _exception_group_leaves(sub)]
    return [exc]


def _mcp_runtime_teardown_noise(exc: BaseException) -> bool:
    """True for known AnyIO/MCP streamable-HTTP races during forced shutdown (e.g. Ctrl+C)."""
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return "cancel scope" in msg or "different task" in msg
    return False


def _mcp_disconnect_teardown_noise(exc: BaseException) -> bool:
    """Whether ``exc`` is only teardown noise from the MCP SDK (safe to log and ignore)."""
    if _mcp_runtime_teardown_noise(exc):
        return True
    if type(exc) is GeneratorExit:
        return True
    if isinstance(exc, BaseExceptionGroup):
        leaves = _exception_group_leaves(exc)
        return bool(leaves) and all(
            _mcp_runtime_teardown_noise(leaf) or type(leaf) is GeneratorExit for leaf in leaves
        )
    return False


class MCPConnectionError(Exception):
    """Connecting or handshaking with an MCP server failed."""

    def __init__(self, server_name: str, message: str | None = None) -> None:
        """Store ``server_name`` and a human-readable message."""
        self.server_name = server_name
        detail = message or f"MCP connection failed for server {server_name!r}"
        super().__init__(detail)


class MCPServerNotConnectedError(Exception):
    """Raised when :meth:`MCPClient.call_tool` references an unknown/disconnected server."""

    def __init__(self, server_name: str) -> None:
        """Attach the unresolved ``server_name``."""
        self.server_name = server_name
        super().__init__(f"MCP server not connected: {server_name!r}")


@dataclass
class MCPServer:
    """Snapshot of configured MCP stdio settings plus discovered tools for one server."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    tools: list[ToolDef]


@dataclass
class _ServerRecord:
    """Runtime connection state per logical server."""

    stack: AsyncExitStack
    session: Any
    tools: list[ToolDef]


@runtime_checkable
class MCPStdioHooks(Protocol):
    """Pluggable transport/session hooks (production SDK or tests)."""

    def stdio_server_parameters(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None,
    ) -> Any:
        """Build stdio server parameters accepted by ``stdio_client``."""
        ...

    def stdio_client(self, params: Any) -> AbstractAsyncContextManager[tuple[Any, Any]]:
        """Return the stdio transport async context manager (read/write streams)."""
        ...

    def client_session(
        self,
        read_stream: Any,
        write_stream: Any,
    ) -> AbstractAsyncContextManager[Any]:
        """Return ``ClientSession`` as an async context manager."""
        ...


class _ProductionStdioHooks:
    """Default hooks that delegate to ``mcp`` (lazy import inside each method body)."""

    def stdio_server_parameters(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None,
    ) -> Any:
        """Instantiate SDK :class:`~mcp.StdioServerParameters`."""
        from mcp import StdioServerParameters

        return StdioServerParameters(command=command, args=args, env=env)

    def stdio_client(self, params: Any) -> AbstractAsyncContextManager[tuple[Any, Any]]:
        """Open stdio subprocess transport."""
        from mcp.client.stdio import stdio_client

        return cast(
            AbstractAsyncContextManager[tuple[Any, Any]],
            stdio_client(params),
        )

    def client_session(
        self,
        read_stream: Any,
        write_stream: Any,
    ) -> AbstractAsyncContextManager[Any]:
        """Open MCP client session over stdio pipes."""
        from mcp import ClientSession

        return cast(
            AbstractAsyncContextManager[Any],
            ClientSession(read_stream, write_stream),
        )


def _tool_input_schema(tool: object) -> dict[str, object]:
    """Extract JSON-schema dict from an MCP ``Tool`` object across SDK revisions."""
    raw = getattr(tool, "inputSchema", None)
    if raw is None:
        raw = getattr(tool, "input_schema", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        dumped = raw.model_dump(mode="python", by_alias=True)
        if isinstance(dumped, dict):
            return dumped
        return {}
    return {}


def _normalize_call_tool_result(result: Any) -> str:
    """Turn ``call_tool`` structured content into a single string."""
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        md = getattr(result, "model_dump", None)
        if callable(md):
            dumped = md(mode="python", by_alias=True)
            try:
                return json.dumps(dumped, default=str)
            except TypeError:
                pass
        return str(result)

    chunks: list[str] = []
    for block in content:
        blk_type = getattr(block, "type", None)
        txt = getattr(block, "text", None)
        if blk_type == "text" and txt is not None:
            chunks.append(str(txt))
            continue
        bmd = getattr(block, "model_dump", None)
        if callable(bmd):
            try:
                chunks.append(json.dumps(bmd(mode="python", by_alias=True), default=str))
            except TypeError:
                chunks.append(str(block))
        else:
            chunks.append(str(block))
    if not chunks:
        md = getattr(result, "model_dump", None)
        if callable(md):
            dumped = md(mode="python", by_alias=True)
            try:
                return json.dumps(dumped, default=str)
            except TypeError:
                pass
        return ""
    return "".join(chunks)


class MCPClient:
    """MCP SDK client (stdio subprocesses and Streamable HTTP) for :class:`monkeybot.core.mcp.ports_mcp.MCPClientPort`."""

    def __init__(self, *, hooks: MCPStdioHooks | None = None) -> None:
        """Create a manager; optionally pass ``hooks`` for tests."""
        self._hooks: MCPStdioHooks = hooks if hooks is not None else _ProductionStdioHooks()
        self._servers: dict[str, _ServerRecord] = {}

    def all_tools(self) -> list[ToolDef]:
        """Return every MCP tool snapshot, grouped by sorted server then tool names."""
        out: list[ToolDef] = []
        for name in sorted(self._servers.keys()):
            rec = self._servers[name]
            out.extend(sorted(rec.tools, key=lambda t: t.name))
        return out

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        """If ``prefixed_name`` matches a connected server, return ``(server_name, tool_name)``.

        Longest server name wins so ``ab__x`` matches server ``ab`` before ``a``.
        """
        for sname in sorted(self._servers.keys(), key=len, reverse=True):
            p = f"{sname}__"
            if prefixed_name.startswith(p):
                return sname, prefixed_name[len(p) :]
        return None

    async def connect(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> list[ToolDef]:
        """Bring up ``name`` via stdio, discover tools, and register ``server__tool`` names."""
        if name in self._servers:
            await self.disconnect(name)
        stack = AsyncExitStack()
        try:
            env_arg: dict[str, str] | None = env if env else None
            params = self._hooks.stdio_server_parameters(command, args, env_arg)
            transport_cm = self._hooks.stdio_client(params)
            read_write = await stack.enter_async_context(transport_cm)
            if not isinstance(read_write, tuple) or len(read_write) != 2:
                raise TypeError(f"stdio_client must yield a 2-tuple, got {read_write!r}")
            read_s, write_s = read_write
            session_cm = self._hooks.client_session(read_s, write_s)
            session = await stack.enter_async_context(session_cm)
            await session.initialize()
            listing = await session.list_tools()
            tools_raw = getattr(listing, "tools", None) or []

            defs: list[ToolDef] = []
            sorted_tools = sorted(
                tools_raw,
                key=lambda t: getattr(t, "name", "") or "",
            )
            for tool in sorted_tools:
                tn = getattr(tool, "name", None)
                if not isinstance(tn, str) or not tn:
                    continue
                prefixed = f"{name}__{tn}"
                desc_val = getattr(tool, "description", None) or ""
                desc = desc_val if isinstance(desc_val, str) else str(desc_val)
                schema = _tool_input_schema(tool)
                defs.append(ToolDef(name=prefixed, description=desc, input_schema=schema))

            self._servers[name] = _ServerRecord(stack=stack, session=session, tools=list(defs))
            return list(defs)
        except MCPConnectionError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise MCPConnectionError(name, str(exc)) from exc

    async def connect_streamable_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[ToolDef]:
        """Connect ``name`` to a remote MCP endpoint (Streamable HTTP); register ``server__tool`` names."""
        if name in self._servers:
            await self.disconnect(name)
        stack = AsyncExitStack()
        try:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            hdr = dict(headers) if headers else {}
            http = httpx.AsyncClient(
                headers=hdr,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, read=600.0),
            )
            await stack.enter_async_context(http)
            transport_cm = streamable_http_client(url, http_client=http)
            read_write = await stack.enter_async_context(transport_cm)
            if not isinstance(read_write, tuple) or len(read_write) != 3:
                raise TypeError(
                    f"streamable_http_client must yield a 3-tuple, got {read_write!r}"
                )
            read_s, write_s, _get_sid = read_write
            session_cm = ClientSession(read_s, write_s)
            session = await stack.enter_async_context(session_cm)
            await session.initialize()
            listing = await session.list_tools()
            tools_raw = getattr(listing, "tools", None) or []

            defs: list[ToolDef] = []
            sorted_tools = sorted(
                tools_raw,
                key=lambda t: getattr(t, "name", "") or "",
            )
            for tool in sorted_tools:
                tn = getattr(tool, "name", None)
                if not isinstance(tn, str) or not tn:
                    continue
                prefixed = f"{name}__{tn}"
                desc_val = getattr(tool, "description", None) or ""
                desc = desc_val if isinstance(desc_val, str) else str(desc_val)
                schema = _tool_input_schema(tool)
                defs.append(ToolDef(name=prefixed, description=desc, input_schema=schema))

            self._servers[name] = _ServerRecord(stack=stack, session=session, tools=list(defs))
            return list(defs)
        except MCPConnectionError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise MCPConnectionError(name, str(exc)) from exc

    async def disconnect(self, name: str) -> None:
        """Tear down one server session; harmless if already disconnected."""
        rec = self._servers.pop(name, None)
        if rec is None:
            return
        try:
            await rec.stack.aclose()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if _mcp_disconnect_teardown_noise(exc):
                logger.debug(
                    "mcp disconnect %s: suppressed SDK teardown noise (%s)",
                    name,
                    exc.__class__.__name__,
                    exc_info=True,
                )
                return
            raise

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: Mapping[str, object],
    ) -> str:
        """Invoke *unprefixed* ``tool_name`` on ``server_name``; return flattened text."""
        rec = self._servers.get(server_name)
        if rec is None:
            raise MCPServerNotConnectedError(server_name)
        result = await rec.session.call_tool(tool_name, arguments=dict(args))
        return _normalize_call_tool_result(result)

    async def load_from_config(self, mcp_json_path: Path) -> None:
        """Load Claude/Cursor ``mcpServers`` JSON; tolerate missing paths and noisy failures."""
        if not mcp_json_path.is_file():
            return

        raw_text = mcp_json_path.read_text(encoding="utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.error("invalid mcp JSON in %s", mcp_json_path)
            return

        servers_any = raw.get("mcpServers")
        if not isinstance(servers_any, dict):
            logger.error(
                "mcp config invalid in %s: expected object mcpServers",
                mcp_json_path,
            )
            return

        for server_name in sorted(servers_any.keys()):
            spec = servers_any[server_name]
            if not isinstance(spec, dict):
                logger.error(
                    "mcp config server %s in %s: expected object entry",
                    server_name,
                    mcp_json_path,
                )
                continue
            if spec.get("enabled") is False:
                continue
            url = spec.get("url")
            if isinstance(url, str) and url.strip():
                headers_any = spec.get("headers") or {}
                headers_out: dict[str, str] = {}
                if isinstance(headers_any, dict):
                    for k, val in headers_any.items():
                        headers_out[str(k)] = "" if val is None else str(val)
                try:
                    await self.connect_streamable_http(
                        server_name,
                        url.strip(),
                        headers_out if headers_out else None,
                    )
                except MCPConnectionError:
                    logger.error(
                        "mcp startup connect failed for server %s (config %s)",
                        server_name,
                        mcp_json_path,
                        exc_info=True,
                    )
                except Exception:
                    logger.error(
                        "mcp startup connect failed for server %s (config %s)",
                        server_name,
                        mcp_json_path,
                        exc_info=True,
                    )
                continue

            command = spec.get("command")
            if not isinstance(command, str) or not command:
                logger.error(
                    "mcp config server %s in %s: missing command or url",
                    server_name,
                    mcp_json_path,
                )
                continue
            args_raw = spec.get("args")
            args_list = list(args_raw) if isinstance(args_raw, list) else []
            args_out = [str(x) for x in args_list]

            env_src = spec.get("env")
            env_out: dict[str, str] = {}
            if isinstance(env_src, dict):
                for k, val in env_src.items():
                    env_out[str(k)] = "" if val is None else str(val)

            try:
                await self.connect(server_name, command, args_out, env_out)
            except MCPConnectionError:
                logger.error(
                    "mcp startup connect failed for server %s (config %s)",
                    server_name,
                    mcp_json_path,
                    exc_info=True,
                )
            except Exception:
                logger.error(
                    "mcp startup connect failed for server %s (config %s)",
                    server_name,
                    mcp_json_path,
                    exc_info=True,
                )
