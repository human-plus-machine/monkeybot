"""MCP client using the official ``mcp`` Python SDK (stdio and Streamable HTTP).

``mcp`` (and ``httpx`` for remote URLs) are imported only inside connection helpers so
``python -c "import monkeybot.core.mcp.mcp_client"`` succeeds without those packages installed.

**call_tool result text:** Returned strings concatenate ``text`` from each content block
whose ``type`` is ``text``. Any other block is JSON-serialized via ``model_dump``
(``by_alias=True``) when present, otherwise :func:`str`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from monkeybot.core.context.tool_result_ingress import (
    dump_model_or_str,
    format_mcp_content_block,
    sanitize_tool_result_text,
)
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


class MCPDiagnosticError(Exception):
    """User-actionable MCP configuration or connectivity failure."""

    def __init__(
        self,
        server_name: str,
        message: str,
        *,
        remedy: str | None = None,
    ) -> None:
        self.server_name = server_name
        self.remedy = remedy
        super().__init__(message)


class MCPAuthError(MCPDiagnosticError):
    """OAuth/token acquisition failed for an MCP HTTP server."""

    pass


class MCPConnectivityError(MCPDiagnosticError):
    """MCP HTTP endpoint rejected the session or could not be reached."""

    pass


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def interpolate_env_vars(value: Any) -> Any:
    """Replace ``${VAR_NAME}`` substrings with ``os.environ`` values (missing → empty string)."""

    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1).strip(), "")

        return _ENV_VAR_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {str(k): interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env_vars(item) for item in value]
    return value


def log_mcp_startup_diagnostic(
    exc: BaseException,
    *,
    server_name: str,
    mcp_json_path: Path,
) -> None:
    """Log a high-signal multi-line banner for MCP startup failures."""
    lines = [
        "",
        "=" * 70,
        "[MCP_STARTUP_FAILURE_DIAGNOSTIC]",
        "=" * 70,
        f"Config file: {mcp_json_path}",
        f"Server name: {server_name}",
        f"Exception:   {exc.__class__.__name__}: {exc}",
    ]
    if isinstance(exc, MCPDiagnosticError) and exc.remedy:
        lines.append(f"Remedy:      {exc.remedy}")
    lines.append("=" * 70)
    logger.error("\n".join(lines))


_MCP_AUTH_HANDLER_CLS: type | None = None


def _mcp_auth_handler_cls() -> Any:
    """Lazily define :class:`httpx.Auth` subclass so importing this module stays light."""
    global _MCP_AUTH_HANDLER_CLS
    if _MCP_AUTH_HANDLER_CLS is not None:
        return _MCP_AUTH_HANDLER_CLS

    import httpx

    class MCPAuthHandler(httpx.Auth):
        """OAuth2 token acquisition for Streamable HTTP MCP (client_credentials / password)."""

        requires_request_body = False
        requires_response_body = False

        def __init__(self, server_name: str, auth_config: Mapping[str, Any]) -> None:
            self.server_name = server_name
            self.config = dict(auth_config)
            self.access_token: str | None = None
            self.expires_at: float = 0.0
            self._lock = asyncio.Lock()

        def _remedy_for_oauth_error(self, error_code: str | None) -> str | None:
            if error_code == "invalid_client":
                return (
                    "Verify client_id and client_secret (or Basic-auth client credentials) "
                    "against your identity provider."
                )
            if error_code == "invalid_scope":
                return "The requested scope is not allowed for this client; adjust `scope`."
            if error_code in {"invalid_grant", "unauthorized_client"}:
                return "The token server rejected this grant; check username/password or client policy."
            return None

        async def _refresh_token_locked(self) -> None:
            import httpx

            flow_raw = self.config.get("flow")
            flow = flow_raw if isinstance(flow_raw, str) else ""
            flow = flow.strip().lower()
            token_url_raw = self.config.get("token_url")
            if not isinstance(token_url_raw, str) or not token_url_raw.strip():
                raise MCPAuthError(
                    self.server_name,
                    "auth.token_url is required and must be a non-empty string.",
                    remedy="Set `token_url` to your OAuth2 token endpoint in mcp.json.",
                )
            token_url = token_url_raw.strip()

            data: dict[str, str] = {}
            if flow == "client_credentials":
                data["grant_type"] = "client_credentials"
                cid = self.config.get("client_id")
                csec = self.config.get("client_secret", "")
                if (self.config.get("client_auth_method") or "body").lower() != "basic":
                    data["client_id"] = "" if cid is None else str(cid)
                    data["client_secret"] = "" if csec is None else str(csec)
            elif flow == "password":
                data["grant_type"] = "password"
                user = self.config.get("username")
                pwd = self.config.get("password")
                if not isinstance(user, str) or not user:
                    raise MCPAuthError(
                        self.server_name,
                        "auth.username is required for flow=password.",
                        remedy="Set `username` (and `password`) under `auth` in mcp.json.",
                    )
                data["username"] = user
                data["password"] = "" if pwd is None else str(pwd)
                if (self.config.get("client_auth_method") or "body").lower() != "basic":
                    cid = self.config.get("client_id")
                    if cid is not None:
                        data["client_id"] = str(cid)
                    csec = self.config.get("client_secret")
                    if csec is not None:
                        data["client_secret"] = str(csec)
            else:
                raise MCPAuthError(
                    self.server_name,
                    f"Unsupported auth.flow {flow_raw!r}; use 'client_credentials' or 'password'.",
                    remedy="Set `flow` to `client_credentials` or `password`.",
                )

            scope = self.config.get("scope")
            if scope is not None and str(scope).strip():
                data["scope"] = str(scope).strip()
            for key in ("audience", "resource"):
                val = self.config.get(key)
                if val is not None and str(val).strip():
                    data[key] = str(val).strip()

            extra = self.config.get("extra")
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if v is None:
                        continue
                    data[str(k)] = str(v)

            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            basic: httpx.Auth | None = None
            if (self.config.get("client_auth_method") or "body").lower() == "basic":
                cid = self.config.get("client_id")
                csec = self.config.get("client_secret", "")
                if cid is None or not str(cid):
                    raise MCPAuthError(
                        self.server_name,
                        "client_id is required when client_auth_method=basic.",
                        remedy="Set `client_id` (and `client_secret`) for HTTP Basic on the token URL.",
                    )
                basic = httpx.BasicAuth(str(cid), str(csec))

            timeout = httpx.Timeout(30.0)
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    if basic is not None:
                        resp = await client.post(
                            token_url, data=data, headers=headers, auth=basic
                        )
                    else:
                        resp = await client.post(token_url, data=data, headers=headers)
            except httpx.RequestError as exc:
                raise MCPAuthError(
                    self.server_name,
                    f"Could not reach token_url ({token_url}): {exc}",
                    remedy="Check network, VPN, DNS, and that token_url is correct.",
                ) from exc

            ct = (resp.headers.get("content-type") or "").lower()
            body: dict[str, Any] = {}
            if "json" in ct or resp.text.strip().startswith("{"):
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        body = parsed
                except json.JSONDecodeError:
                    body = {}
            if resp.status_code >= 400:
                err = body.get("error") if isinstance(body.get("error"), str) else None
                desc = body.get("error_description")
                desc_s = desc if isinstance(desc, str) else None
                detail = desc_s or resp.text or f"HTTP {resp.status_code}"
                remedy = self._remedy_for_oauth_error(err)
                raise MCPAuthError(
                    self.server_name,
                    f"Token endpoint returned {resp.status_code} ({err or 'no_error_code'}): {detail}",
                    remedy=remedy,
                )

            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise MCPAuthError(
                    self.server_name,
                    "Token response did not include a usable access_token string.",
                    remedy="Inspect the token server response format; monkeybot expects OAuth2 JSON.",
                )
            self.access_token = token
            expires_in = body.get("expires_in", 3600)
            try:
                ttl = float(expires_in)
            except (TypeError, ValueError):
                ttl = 3600.0
            self.expires_at = time.time() + max(30.0, ttl)

        async def _get_token(self) -> str:
            async with self._lock:
                now = time.time()
                if self.access_token and now < self.expires_at - 60:
                    return self.access_token
                await self._refresh_token_locked()
                if not self.access_token:
                    raise MCPAuthError(
                        self.server_name,
                        "Token refresh completed without an access_token.",
                        remedy="Check auth configuration and token server behavior.",
                    )
                return self.access_token

        async def async_auth_flow(
            self, request: httpx.Request
        ) -> AsyncGenerator[httpx.Request, httpx.Response]:
            token = await self._get_token()
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
            if response.status_code == 401:
                async with self._lock:
                    if self.access_token == token:
                        self.access_token = None
                        self.expires_at = 0.0
                        await self._refresh_token_locked()
                if not self.access_token:
                    raise MCPAuthError(
                        self.server_name,
                        "Token refresh after HTTP 401 did not yield an access_token.",
                        remedy="Verify the MCP server accepts this token audience/scope.",
                    )
                request.headers["Authorization"] = f"Bearer {self.access_token}"
                yield request

    _MCP_AUTH_HANDLER_CLS = MCPAuthHandler
    return MCPAuthHandler


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
        return sanitize_tool_result_text(dump_model_or_str(result))

    chunks: list[str] = []
    for block in content:
        chunks.append(format_mcp_content_block(block))
    if not chunks:
        dumped = dump_model_or_str(result) if getattr(result, "model_dump", None) else ""
        return sanitize_tool_result_text(dumped)
    return sanitize_tool_result_text("".join(chunks))


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
        *,
        auth: Any | None = None,
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
            if auth is not None:
                hdr.pop("Authorization", None)
                hdr.pop("authorization", None)
            http = httpx.AsyncClient(
                headers=hdr if hdr else None,
                auth=auth,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, read=600.0),
            )
            await stack.enter_async_context(http)
            transport_cm = streamable_http_client(url, http_client=http)
            read_write = await stack.enter_async_context(transport_cm)
            if not isinstance(read_write, tuple) or len(read_write) != 3:
                raise TypeError(f"streamable_http_client must yield a 3-tuple, got {read_write!r}")
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
        except MCPAuthError:
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

    async def disconnect_all(self) -> None:
        """Disconnect every connected MCP server."""
        for name in list(self._servers.keys()):
            await self.disconnect(name)

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

    async def load_from_config(
        self,
        mcp_json_path: Path,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Load Claude/Cursor ``mcpServers`` JSON; tolerate missing paths and noisy failures.

        Replaces ``${ENV_NAME}`` substrings with environment values (missing → empty string).

        Streamable HTTP entries may include an ``auth`` object with ``flow`` set to
        ``client_credentials`` or ``password`` plus ``token_url`` and related fields.

        Args:
            mcp_json_path: Path to ``mcp.json``.
            raise_on_error: When True, abort on the first per-server startup failure after
                emitting a diagnostic banner. When False, log and continue with other servers.
        """
        if not mcp_json_path.is_file():
            return

        raw_text = mcp_json_path.read_text(encoding="utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.error("invalid mcp JSON in %s", mcp_json_path)
            return

        if isinstance(raw, dict):
            raw = interpolate_env_vars(raw)
        else:
            logger.error("mcp config invalid in %s: expected top-level object", mcp_json_path)
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

                auth_handler: Any | None = None
                auth_any = spec.get("auth")
                if isinstance(auth_any, dict) and auth_any:
                    handler_cls = _mcp_auth_handler_cls()
                    auth_handler = handler_cls(server_name, auth_any)
                    headers_out.pop("Authorization", None)
                    headers_out.pop("authorization", None)

                try:
                    await self.connect_streamable_http(
                        server_name,
                        url.strip(),
                        headers_out if headers_out else None,
                        auth=auth_handler,
                    )
                except MCPAuthError as exc:
                    log_mcp_startup_diagnostic(
                        exc, server_name=server_name, mcp_json_path=mcp_json_path
                    )
                    logger.error(
                        "mcp startup connect failed for server %s (config %s)",
                        server_name,
                        mcp_json_path,
                        exc_info=True,
                    )
                    if raise_on_error:
                        raise
                except MCPConnectionError as exc:
                    log_mcp_startup_diagnostic(
                        exc, server_name=server_name, mcp_json_path=mcp_json_path
                    )
                    logger.error(
                        "mcp startup connect failed for server %s (config %s)",
                        server_name,
                        mcp_json_path,
                        exc_info=True,
                    )
                    if raise_on_error:
                        raise
                except Exception as exc:
                    log_mcp_startup_diagnostic(
                        exc, server_name=server_name, mcp_json_path=mcp_json_path
                    )
                    logger.error(
                        "mcp startup connect failed for server %s (config %s)",
                        server_name,
                        mcp_json_path,
                        exc_info=True,
                    )
                    if raise_on_error:
                        raise
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
            except MCPConnectionError as exc:
                log_mcp_startup_diagnostic(
                    exc, server_name=server_name, mcp_json_path=mcp_json_path
                )
                logger.error(
                    "mcp startup connect failed for server %s (config %s)",
                    server_name,
                    mcp_json_path,
                    exc_info=True,
                )
                if raise_on_error:
                    raise
            except Exception as exc:
                log_mcp_startup_diagnostic(
                    exc, server_name=server_name, mcp_json_path=mcp_json_path
                )
                logger.error(
                    "mcp startup connect failed for server %s (config %s)",
                    server_name,
                    mcp_json_path,
                    exc_info=True,
                )
                if raise_on_error:
                    raise
