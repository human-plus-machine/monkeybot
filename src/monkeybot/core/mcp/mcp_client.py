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

from monkeybot.core.context.tool_output_policy import (
    register_mcp_tool_names,
    unregister_mcp_server_tools,
)
from monkeybot.core.context.tool_result_ingress import (
    dump_model_or_str,
    format_mcp_content_block,
    sanitize_tool_result_text,
)
from monkeybot.core.logging_utils import kv
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

# Unprefixed tool name that cloud-browser MCP servers (e.g. browser-harness) expose
# to stop a remote session before it accrues further billing. Disconnecting the
# stdio transport alone never reaches the remote session, so disconnect() calls
# this tool first, best-effort, whenever a connected server exposes it.
_BROWSER_STOP_TOOL = "browser_stop"
_BROWSER_STOP_TIMEOUT_S = 10.0


def interpolate_env_vars(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace ``${VAR_NAME}`` with overlay+process env (missing → empty string).

    ``env`` (typically a ``RuntimeConfig.env_values`` snapshot) wins over
    ``os.environ`` so YAML-backed keys interpolate after in-process reload
    without waiting for process env to be overwritten.
    """
    source: Mapping[str, str] = (
        os.environ if env is None else {**os.environ, **dict(env)}
    )

    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            return source.get(match.group(1).strip(), "")

        return _ENV_VAR_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {str(k): interpolate_env_vars(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env_vars(item, env) for item in value]
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
                        resp = await client.post(token_url, data=data, headers=headers, auth=basic)
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
    capabilities: dict[str, bool] | None = None


@dataclass(frozen=True)
class MCPCatalogApplyResult:
    """Outcome of :meth:`MCPClient.apply_catalog_diff` (untouched children stay up)."""

    reconnected: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()


# Status machine values (OpenCode-inspired; progressive-disclosure aware).
MCP_STATUS_CONNECTED = "connected"
MCP_STATUS_CATALOGUED = "catalogued"
MCP_STATUS_DISCONNECTED = "disconnected"
MCP_STATUS_DISABLED = "disabled"
MCP_STATUS_FAILED = "failed"
MCP_STATUS_NEEDS_AUTH = "needs_auth"


def _server_capabilities_snapshot(session: Any) -> dict[str, bool]:
    """Return a flat capability map from an initialized MCP session."""
    caps = None
    getter = getattr(session, "get_server_capabilities", None)
    if callable(getter):
        caps = getter()
    if caps is None:
        return {}
    out: dict[str, bool] = {}
    for key in ("tools", "resources", "prompts"):
        val = getattr(caps, key, None)
        out[key] = val is not None
    return out


def _dump_mcp_model(obj: Any) -> dict[str, Any]:
    """Best-effort JSON-friendly dump of an MCP SDK model / namespace."""
    if obj is None:
        return {}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    if isinstance(obj, dict):
        return dict(obj)
    raw = getattr(obj, "__dict__", None)
    if not isinstance(raw, dict):
        raw = {}
    slots = getattr(type(obj), "__slots__", None)
    if isinstance(slots, str):
        slots = (slots,)
    if slots:
        raw = dict(raw)
        for key in slots:
            if key not in raw and hasattr(obj, key):
                raw[key] = getattr(obj, key)
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key, val in raw.items():
        if key.startswith("_") or val is None:
            continue
        out[key] = str(val) if key == "uri" else val
    return out


def _normalize_resource_read(result: Any) -> dict[str, Any]:
    """Flatten ``read_resource`` result into text + structured contents."""
    contents_raw = getattr(result, "contents", None) or []
    text_chunks: list[str] = []
    contents: list[dict[str, Any]] = []
    for block in contents_raw:
        entry = _dump_mcp_model(block)
        text_val = getattr(block, "text", None)
        blob_val = getattr(block, "blob", None)
        if isinstance(text_val, str):
            text_chunks.append(text_val)
            entry["text"] = text_val
        elif blob_val is not None:
            # Keep a short note rather than dumping large base64 into the turn.
            mime = (
                getattr(block, "mimeType", None)
                or entry.get("mimeType")
                or "application/octet-stream"
            )
            uri = str(getattr(block, "uri", "") or entry.get("uri") or "")
            note = f"[Binary MCP resource omitted: {uri} ({mime})]"
            text_chunks.append(note)
            entry["blob_omitted"] = True
            entry["mimeType"] = mime
        contents.append(entry)
    text = sanitize_tool_result_text("".join(text_chunks))
    return {"text": text, "contents": contents}


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


def _unpack_streamable_http_streams(streams: Any) -> tuple[Any, Any]:
    """Return ``(read, write)`` from an MCP 1.x 3-tuple or 2.x 2-tuple."""
    if isinstance(streams, tuple) and len(streams) in (2, 3):
        return streams[0], streams[1]
    raise TypeError(f"streamable_http_client must yield a 2-tuple or 3-tuple, got {streams!r}")


class MCPClient:
    """MCP SDK client (stdio subprocesses and Streamable HTTP) for :class:`monkeybot.core.mcp.ports_mcp.MCPClientPort`."""

    def __init__(self, *, hooks: MCPStdioHooks | None = None) -> None:
        """Create a manager; optionally pass ``hooks`` for tests."""
        self._hooks: MCPStdioHooks = hooks if hooks is not None else _ProductionStdioHooks()
        self._servers: dict[str, _ServerRecord] = {}
        # Catalogued mcp.json servers; connected only via enable_mcp / connect_from_catalog.
        self._catalog: dict[str, dict[str, Any]] = {}
        self._config_path: Path | None = None
        # Catalog + ever-connected names (including ad-hoc); used for tool-list refresh.
        self._seen_servers: set[str] = set()
        # Last-known status per server (catalogued / connected / failed / …).
        self._statuses: dict[str, dict[str, Any]] = {}
        # Snapshot env overlay for ``${VAR}`` interpolation (YAML-backed keys).
        self._env_overlay: Mapping[str, str] | None = None

    def set_env_overlay(self, env: Mapping[str, str] | None) -> None:
        """Use snapshot env values when interpolating ``mcp.json`` ``${VAR}`` refs."""
        self._env_overlay = env

    def _set_status(self, name: str, status: str, **extra: Any) -> None:
        """Record lifecycle status for ``name`` (overwrites prior entry)."""
        entry: dict[str, Any] = {"name": name, "status": status}
        entry.update({k: v for k, v in extra.items() if v is not None})
        self._statuses[name] = entry

    def _record_connect_failure(self, name: str, exc: BaseException) -> None:
        """Update status after a failed connect attempt."""
        if isinstance(exc, MCPAuthError):
            self._set_status(name, MCP_STATUS_NEEDS_AUTH, error=str(exc))
        else:
            self._set_status(name, MCP_STATUS_FAILED, error=str(exc))

    def all_tools(self) -> list[ToolDef]:
        """Return every MCP tool snapshot, grouped by sorted server then tool names."""
        out: list[ToolDef] = []
        for name in sorted(self._servers.keys()):
            rec = self._servers[name]
            out.extend(sorted(rec.tools, key=lambda t: t.name))
        return out

    def catalog_names(self) -> list[str]:
        """Return sorted names of servers known from the last ``load_from_config``."""
        return sorted(self._catalog.keys())

    def known_server_names(self) -> list[str]:
        """Return sorted catalog + ever-connected server names."""
        return sorted(self._seen_servers)

    def is_connected(self, name: str) -> bool:
        """True when ``name`` has an active MCP session."""
        return name in self._servers

    def status(self, name: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        """Return server status snapshot(s).

        Without ``name``, returns every tracked server sorted by name. With ``name``,
        returns that server's entry or a synthetic ``disconnected`` stub.
        """
        if name is not None:
            if name in self._statuses:
                return dict(self._statuses[name])
            if name in self._servers:
                return {"name": name, "status": MCP_STATUS_CONNECTED}
            if name in self._catalog:
                return {"name": name, "status": MCP_STATUS_CATALOGUED}
            return {"name": name, "status": MCP_STATUS_DISCONNECTED}
        names = sorted(set(self._statuses) | set(self._catalog) | set(self._servers))
        return [self.status(n) for n in names]  # type: ignore[misc]

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

            self._servers[name] = _ServerRecord(
                stack=stack,
                session=session,
                tools=list(defs),
                capabilities=_server_capabilities_snapshot(session),
            )
            self._seen_servers.add(name)
            register_mcp_tool_names(t.name for t in defs)
            self._set_status(
                name,
                MCP_STATUS_CONNECTED,
                tool_count=len(defs),
                capabilities=self._servers[name].capabilities,
            )
            return list(defs)
        except MCPConnectionError as exc:
            await stack.aclose()
            self._record_connect_failure(name, exc)
            raise
        except Exception as exc:
            await stack.aclose()
            wrapped = MCPConnectionError(name, str(exc))
            self._record_connect_failure(name, wrapped)
            raise wrapped from exc

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
            # mcp 2.x wants httpx2.AsyncClient here; stay on httpx until that migration.
            http = httpx.AsyncClient(
                headers=hdr if hdr else None,
                auth=auth,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, read=600.0),
            )
            await stack.enter_async_context(http)
            transport_cm = streamable_http_client(url, http_client=http)
            read_write = await stack.enter_async_context(transport_cm)
            read_s, write_s = _unpack_streamable_http_streams(read_write)
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

            self._servers[name] = _ServerRecord(
                stack=stack,
                session=session,
                tools=list(defs),
                capabilities=_server_capabilities_snapshot(session),
            )
            self._seen_servers.add(name)
            register_mcp_tool_names(t.name for t in defs)
            self._set_status(
                name,
                MCP_STATUS_CONNECTED,
                tool_count=len(defs),
                capabilities=self._servers[name].capabilities,
            )
            return list(defs)
        except MCPConnectionError as exc:
            await stack.aclose()
            self._record_connect_failure(name, exc)
            raise
        except MCPAuthError as exc:
            await stack.aclose()
            self._record_connect_failure(name, exc)
            raise
        except Exception as exc:
            await stack.aclose()
            wrapped = MCPConnectionError(name, str(exc))
            self._record_connect_failure(name, wrapped)
            raise wrapped from exc

    async def _stop_remote_session_before_teardown(self, name: str, rec: _ServerRecord) -> None:
        """Best-effort ``browser_stop`` call so cloud-browser billing ends on disconnect.

        Closing the stdio transport only kills the local MCP subprocess; a remote
        Browser Use Cloud session (or any other server's remote resource behind a
        ``browser_stop`` tool) keeps running otherwise. Runs before ``stack.aclose()``
        with a short timeout so a hung remote call never blocks shutdown.
        """
        prefixed_stop_tool = f"{name}__{_BROWSER_STOP_TOOL}"
        if not any(t.name == prefixed_stop_tool for t in rec.tools):
            return
        try:
            async with asyncio.timeout(_BROWSER_STOP_TIMEOUT_S):
                await rec.session.call_tool(_BROWSER_STOP_TOOL, arguments={})
        except Exception as exc:
            logger.warning(
                "mcp disconnect %s: %s call failed (remote session may keep billing): %s",
                name,
                _BROWSER_STOP_TOOL,
                exc,
            )

    async def disconnect(self, name: str) -> None:
        """Tear down one server session; harmless if already disconnected."""
        rec = self._servers.pop(name, None)
        if rec is None:
            return
        unregister_mcp_server_tools(name)
        try:
            await self._stop_remote_session_before_teardown(name, rec)
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
                else:
                    raise
        finally:
            if name in self._catalog:
                self._set_status(name, MCP_STATUS_CATALOGUED)
            else:
                self._set_status(name, MCP_STATUS_DISCONNECTED)

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

    def _require_connected(self, server_name: str) -> _ServerRecord:
        rec = self._servers.get(server_name)
        if rec is None:
            raise MCPServerNotConnectedError(server_name)
        return rec

    def _connected_server_names(self, server_name: str | None = None) -> list[str]:
        if server_name is not None:
            if server_name not in self._servers:
                raise MCPServerNotConnectedError(server_name)
            return [server_name]
        return sorted(self._servers.keys())

    async def _list_capability_items(
        self,
        *,
        server_name: str | None,
        capability: str,
        list_method: str,
        result_attr: str,
        missing_remedy: str,
    ) -> list[dict[str, Any]]:
        """List resources/prompts/templates from connected servers that advertise ``capability``."""
        out: list[dict[str, Any]] = []
        for sname in self._connected_server_names(server_name):
            rec = self._servers[sname]
            caps = rec.capabilities or {}
            if caps and not caps.get(capability, False):
                if server_name is not None:
                    raise MCPDiagnosticError(
                        sname,
                        f"MCP server {sname!r} does not advertise {capability} capability",
                        remedy=missing_remedy,
                    )
                continue
            listing = await getattr(rec.session, list_method)()
            for item in getattr(listing, result_attr, None) or []:
                entry = _dump_mcp_model(item)
                entry["server"] = sname
                if "uri" in entry:
                    entry["uri"] = str(entry["uri"])
                out.append(entry)
        return out

    async def list_resources(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List MCP resources from one or all connected servers."""
        return await self._list_capability_items(
            server_name=server_name,
            capability="resources",
            list_method="list_resources",
            result_attr="resources",
            missing_remedy="Use a server that supports resources, or call enable_mcp first.",
        )

    async def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        """Read one MCP resource by ``server_name`` + ``uri``."""
        rec = self._require_connected(server_name)
        caps = rec.capabilities or {}
        if caps and not caps.get("resources", False):
            raise MCPDiagnosticError(
                server_name,
                f"MCP server {server_name!r} does not advertise resources capability",
                remedy="Pick a resource-capable server from list_mcp_resources, or call enable_mcp.",
            )
        from pydantic import AnyUrl

        result = await rec.session.read_resource(AnyUrl(uri))
        normalized = _normalize_resource_read(result)
        return {
            "server": server_name,
            "uri": uri,
            "text": normalized["text"],
            "contents": normalized["contents"],
        }

    async def list_prompts(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List MCP prompts from one or all connected servers."""
        return await self._list_capability_items(
            server_name=server_name,
            capability="prompts",
            list_method="list_prompts",
            result_attr="prompts",
            missing_remedy="Use a server that supports prompts, or call enable_mcp first.",
        )

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch a named MCP prompt template (with optional string arguments)."""
        rec = self._require_connected(server_name)
        caps = rec.capabilities or {}
        if caps and not caps.get("prompts", False):
            raise MCPDiagnosticError(
                server_name,
                f"MCP server {server_name!r} does not advertise prompts capability",
                remedy="Pick a prompt-capable server from list_mcp_prompts, or call enable_mcp.",
            )
        args_out = {str(k): str(v) for k, v in dict(arguments or {}).items()}
        result = await rec.session.get_prompt(prompt_name, arguments=args_out or None)
        messages: list[dict[str, Any]] = []
        for msg in getattr(result, "messages", None) or []:
            messages.append(_dump_mcp_model(msg))
        description = getattr(result, "description", None)
        return {
            "server": server_name,
            "name": prompt_name,
            "description": description if isinstance(description, str) else None,
            "messages": messages,
        }

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        """Connect a server previously registered by :meth:`load_from_config`.

        Re-reads ``mcp.json`` so late-bound env (e.g. Monkeyapp ``BU_CDP_URL``) is
        applied. Already-connected servers reconnect only when their resolved
        catalog spec changed; otherwise this is a no-op that returns current tools.
        Raises :class:`MCPDiagnosticError` when ``name`` is not in the catalog.
        """
        previous = dict(self._catalog[name]) if name in self._catalog else None
        if self._config_path is not None:
            # Refresh catalog from disk without dropping other connected servers.
            await self._reload_catalog_entry(name)
        if name in self._servers:
            current = self._catalog.get(name)
            if previous is not None and previous == current:
                return list(self._servers[name].tools)
            await self.disconnect(name)
        spec = self._catalog.get(name)
        if spec is None:
            known = self.catalog_names()
            known_msg = ", ".join(known) if known else "(none)"
            raise MCPDiagnosticError(
                name,
                f"Unknown MCP server {name!r}. Known configured servers: {known_msg}",
                remedy=("Use a name from mcp.json (after load_from_config), then call enable_mcp."),
            )
        defs = await self._connect_from_spec(
            name, spec, mcp_json_path=self._config_path, raise_on_error=True
        )
        return defs

    async def _reload_catalog_entry(self, name: str) -> None:
        """Re-parse mcp.json and replace one catalog entry (env interpolation included)."""
        path = self._config_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("mcp catalog refresh failed reading %s", path, exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        raw = interpolate_env_vars(raw, self._env_overlay)
        servers_any = raw.get("mcpServers")
        if not isinstance(servers_any, dict):
            return
        spec = servers_any.get(name)
        if not isinstance(spec, dict):
            self._catalog.pop(name, None)
            return
        if spec.get("enabled") is False:
            self._catalog.pop(name, None)
            return
        self._catalog[name] = dict(spec)

    def _log_connect_failure(
        self,
        exc: BaseException,
        *,
        server_name: str,
        config_path: Path,
    ) -> None:
        log_mcp_startup_diagnostic(exc, server_name=server_name, mcp_json_path=config_path)
        logger.error(
            "mcp connect failed for server %s (config %s)",
            server_name,
            config_path,
            exc_info=True,
        )

    async def _connect_from_spec(
        self,
        server_name: str,
        spec: Mapping[str, Any],
        *,
        mcp_json_path: Path | None,
        raise_on_error: bool,
    ) -> list[ToolDef]:
        """Connect stdio or Streamable HTTP from one mcpServers entry."""
        config_path = mcp_json_path if mcp_json_path is not None else Path("<unknown>")
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
                defs = await self.connect_streamable_http(
                    server_name,
                    url.strip(),
                    headers_out if headers_out else None,
                    auth=auth_handler,
                )
            except Exception as exc:
                self._log_connect_failure(exc, server_name=server_name, config_path=config_path)
                if raise_on_error:
                    raise
                return []
            logger.info(
                "mcp catalog server connected %s",
                kv(server=server_name, tools=len(defs), transport="http"),
            )
            return defs

        command = spec.get("command")
        if not isinstance(command, str) or not command:
            logger.error(
                "mcp config server %s in %s: missing command or url",
                server_name,
                config_path,
            )
            if raise_on_error:
                raise MCPDiagnosticError(
                    server_name,
                    f"MCP server {server_name!r} is missing command or url",
                    remedy="Add a stdio command/args or a Streamable HTTP url in mcp.json.",
                )
            return []
        args_raw = spec.get("args")
        args_list = list(args_raw) if isinstance(args_raw, list) else []
        args_out = [str(x) for x in args_list]

        env_src = spec.get("env")
        env_out: dict[str, str] = {}
        if isinstance(env_src, dict):
            for k, val in env_src.items():
                env_out[str(k)] = "" if val is None else str(val)

        try:
            defs = await self.connect(server_name, command, args_out, env_out)
        except Exception as exc:
            self._log_connect_failure(exc, server_name=server_name, config_path=config_path)
            if raise_on_error:
                raise
            return []
        logger.info(
            "mcp catalog server connected %s",
            kv(server=server_name, tools=len(defs), transport="stdio"),
        )
        return defs

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

        Every server under ``mcpServers`` is registered in the catalog for
        :meth:`connect_from_catalog` / ``enable_mcp`` unless ``"enabled": false``.
        Catalogued servers are **not** connected at startup by default (progressive
        disclosure — the model opts in via ``enable_mcp``). Set ``"autoConnect": true``
        to restore eager startup connect for that server. Remove an entry from the
        file to drop it from the catalog.

        Args:
            mcp_json_path: Path to ``mcp.json``.
            raise_on_error: When True, invalid JSON or structure raises
                :class:`MCPDiagnosticError` instead of logging and returning.
        """
        if not mcp_json_path.is_file():
            return

        servers_any = self._read_mcp_servers_file(mcp_json_path, raise_on_error=raise_on_error)
        if servers_any is None:
            return

        self._config_path = mcp_json_path
        self._catalog.clear()
        new_catalog, disabled = self._parse_catalog_entries(
            servers_any, mcp_json_path, raise_on_error=raise_on_error
        )
        for server_name in sorted(disabled):
            logger.info("mcp server skipped (enabled: false) %s", kv(server=server_name))
            self._set_status(server_name, MCP_STATUS_DISABLED)
        for server_name, spec in new_catalog.items():
            self._catalog[server_name] = spec
            self._seen_servers.add(server_name)
            if spec.get("autoConnect") is True:
                logger.info(
                    "mcp autoConnect server=%s (connecting at startup)",
                    server_name,
                )
                await self._connect_from_spec(
                    server_name,
                    spec,
                    mcp_json_path=mcp_json_path,
                    raise_on_error=raise_on_error,
                )
            else:
                self._set_status(server_name, MCP_STATUS_CATALOGUED)
                logger.info(
                    "mcp catalog registered server=%s (connect via enable_mcp)",
                    server_name,
                )

    def _read_mcp_servers_file(
        self,
        mcp_json_path: Path,
        *,
        raise_on_error: bool,
    ) -> dict[str, Any] | None:
        """Parse interpolated ``mcpServers`` without mutating catalog/connections.

        Returns ``None`` when the file is missing or invalid (and ``raise_on_error``
        is False). An empty dict means a valid file with no servers.
        """
        if not mcp_json_path.is_file():
            return None

        raw_text = mcp_json_path.read_text(encoding="utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error("invalid mcp JSON %s", kv(path=str(mcp_json_path)))
            if raise_on_error:
                raise MCPDiagnosticError(
                    "<mcp.json>",
                    f"Invalid MCP JSON in {mcp_json_path}: {exc}",
                    remedy="Fix mcp.json so it parses as JSON.",
                ) from exc
            return None

        if isinstance(raw, dict):
            raw = interpolate_env_vars(raw, self._env_overlay)
        else:
            logger.error(
                "mcp config invalid: expected top-level object %s",
                kv(path=str(mcp_json_path)),
            )
            if raise_on_error:
                raise MCPDiagnosticError(
                    "<mcp.json>",
                    f"MCP config in {mcp_json_path} must be a JSON object",
                    remedy="Wrap servers under a top-level object with mcpServers.",
                )
            return None

        servers_any = raw.get("mcpServers")
        if not isinstance(servers_any, dict):
            logger.error(
                "mcp config invalid: expected object mcpServers %s",
                kv(path=str(mcp_json_path)),
            )
            if raise_on_error:
                raise MCPDiagnosticError(
                    "<mcp.json>",
                    f"MCP config in {mcp_json_path} must include an object mcpServers",
                    remedy="Add a mcpServers object mapping server names to specs.",
                )
            return None
        return servers_any

    def _parse_catalog_entries(
        self,
        servers_any: dict[str, Any],
        mcp_json_path: Path,
        *,
        raise_on_error: bool,
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Split interpolated ``mcpServers`` into enabled specs and disabled names."""
        new_catalog: dict[str, dict[str, Any]] = {}
        disabled: set[str] = set()
        for server_name in sorted(servers_any.keys()):
            spec = servers_any[server_name]
            if not isinstance(spec, dict):
                logger.error(
                    "mcp config server expected object entry %s",
                    kv(server=server_name, path=str(mcp_json_path)),
                )
                if raise_on_error:
                    raise MCPDiagnosticError(
                        server_name,
                        f"MCP server {server_name!r} entry must be an object",
                        remedy="Fix the server entry in mcp.json.",
                    )
                continue
            if spec.get("enabled") is False:
                disabled.add(server_name)
                continue
            new_catalog[server_name] = dict(spec)
        return new_catalog, disabled

    async def apply_catalog_diff(
        self,
        mcp_json_path: Path,
        *,
        raise_on_error: bool = False,
    ) -> MCPCatalogApplyResult:
        """Reconnect only added/changed/removed servers; leave untouched children running.

        Invalid JSON leaves the live catalog and connections alone. A missing file
        is treated as an empty catalog (every connected server is disconnected).
        """
        parsed = self._read_mcp_servers_file(mcp_json_path, raise_on_error=raise_on_error)
        if parsed is None and mcp_json_path.is_file():
            return MCPCatalogApplyResult(kept=tuple(sorted(self._servers)))
        servers_any: dict[str, Any] = parsed if parsed is not None else {}
        new_catalog, disabled = self._parse_catalog_entries(
            servers_any, mcp_json_path, raise_on_error=raise_on_error
        )

        old_catalog = dict(self._catalog)
        old_connected = set(self._servers)
        removed = set(old_catalog) - set(new_catalog)
        added = set(new_catalog) - set(old_catalog)
        changed = {n for n in set(old_catalog) & set(new_catalog) if old_catalog[n] != new_catalog[n]}

        for name in sorted(removed | changed | disabled):
            if name in self._servers:
                await self.disconnect(name)

        self._config_path = mcp_json_path
        for name in removed:
            self._catalog.pop(name, None)
            if name not in disabled:
                self._set_status(name, MCP_STATUS_DISCONNECTED)
        for name in disabled:
            self._catalog.pop(name, None)
            self._set_status(name, MCP_STATUS_DISABLED)
        for name, spec in new_catalog.items():
            self._catalog[name] = spec
            self._seen_servers.add(name)
            if name not in self._servers:
                self._set_status(name, MCP_STATUS_CATALOGUED)

        reconnected: list[str] = []
        for name in sorted(changed | added):
            spec = new_catalog[name]
            if spec.get("autoConnect") is True or (name in old_connected and name in changed):
                try:
                    await self._connect_from_spec(
                        name,
                        spec,
                        mcp_json_path=mcp_json_path,
                        raise_on_error=raise_on_error,
                    )
                    if name in self._servers:
                        reconnected.append(name)
                except Exception:
                    logger.exception(
                        "mcp catalog diff reconnect failed %s",
                        kv(server=name, path=str(mcp_json_path)),
                    )
                    if raise_on_error:
                        raise

        kept = tuple(
            sorted(n for n in old_connected if n in self._servers and n not in reconnected)
        )
        result = MCPCatalogApplyResult(
            reconnected=tuple(reconnected),
            kept=kept,
            removed=tuple(sorted(removed | disabled)),
            added=tuple(sorted(added)),
        )
        logger.info(
            "mcp catalog diff applied %s",
            kv(
                reconnected=",".join(result.reconnected),
                kept=",".join(result.kept),
                removed=",".join(result.removed),
            ),
        )
        return result
