"""Tests for ``monkeybot.core.mcp.mcp_client``. Uses injectable hooks; no real ``mcp`` subprocess."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from monkeybot.core.mcp import mcp_client as mc
from monkeybot.core.mcp.mcp_client import MCPClient, MCPConnectionError


class _SessionACM:
    """Async context manager that yields ``session_impl``."""

    def __init__(self, session_impl: SimpleNamespace, *, on_enter: AsyncMock | None = None):
        """Store the session backing object and optional enter hook."""
        self._sess = session_impl
        self._on_enter = on_enter

    async def __aenter__(self) -> SimpleNamespace:
        """Expose the MCP session surrogate."""
        if self._on_enter is not None:
            await self._on_enter()
        return self._sess

    async def __aexit__(self, *_exc: object) -> bool | None:
        """Close transports (no-op for tests)."""
        return None


class _CrashHooks:

    def stdio_server_parameters(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None,
    ) -> SimpleNamespace:
        """Raise if invoked (missing config paths must short-circuit)."""
        raise AssertionError("connect should never run without mcp.json on disk")

    def stdio_client(self, params: object):  # noqa: ANN202
        """Raise if invoked."""
        raise AssertionError("connect should never run without mcp.json on disk")

    def client_session(self, read_stream: object, write_stream: object) -> _SessionACM:
        """Raise if invoked."""
        raise AssertionError("connect should never run without mcp.json on disk")


def _stub_hooks(session: SimpleNamespace) -> mc.MCPStdioHooks:

    class _Hooks:
        def stdio_server_parameters(
            self,
            command: str,
            args: list[str],
            env: dict[str, str] | None,
        ) -> SimpleNamespace:
            return SimpleNamespace(command=command, args=args, env=env)

        def stdio_client(self, params: object):  # noqa: ANN202
            @asynccontextmanager
            async def _transport():  # noqa: ANN202
                yield ("fake-read", "fake-write")

            return _transport()

        def client_session(self, read_stream: object, write_stream: object) -> _SessionACM:
            return _SessionACM(session)

    return _Hooks()


@pytest.mark.asyncio
async def test_connect_prefixes_tool_names() -> None:
    """``connect`` returns ``server__tool`` ``ToolDef`` names."""
    fs_tool_read = SimpleNamespace(name="read_file", description="d1", inputSchema={})
    fs_tool_write = SimpleNamespace(name="write_file", description="d2", inputSchema={})
    listing = SimpleNamespace(tools=[fs_tool_write, fs_tool_read])

    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)

    hooks = _stub_hooks(sess)
    client = MCPClient(hooks=hooks)
    out = await client.connect("fs", "python", ["-m", "srv"], {})

    names = sorted(t.name for t in out)
    assert names == ["fs__read_file", "fs__write_file"]
    all_names = sorted(t.name for t in client.all_tools())
    assert all_names == names


@pytest.mark.asyncio
async def test_call_tool_returns_string() -> None:
    """``call_tool`` flattens text content to a bare string."""
    listing = SimpleNamespace(
        tools=[SimpleNamespace(name="read_file", description="", inputSchema={})]
    )
    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)

    sess.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")]
        )
    )

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("fs", "python", [], {})
    resolved = await client.call_tool("fs", "read_file", {})

    assert resolved == "ok"
    sess.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_removes_tools() -> None:
    """Disconnected servers drop out of ``all_tools``; repeat disconnect is tolerated."""
    listing = SimpleNamespace(
        tools=[
            SimpleNamespace(name="only", description="x", inputSchema={"type": "object"}),
        ]
    )

    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("fs", "python", [], {})
    assert len(client.all_tools()) == 1
    await client.disconnect("fs")
    assert client.all_tools() == []
    await client.disconnect("fs")
    assert client.all_tools() == []


@pytest.mark.asyncio
async def test_disconnect_suppresses_anyio_cancel_scope_teardown_noise() -> None:
    """Ctrl+C / uvicorn shutdown can trigger MCP streamable_http + AnyIO teardown races."""
    listing = SimpleNamespace(
        tools=[SimpleNamespace(name="only", description="x", inputSchema={"type": "object"})]
    )
    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("fs", "python", [], {})
    rec = client._servers["fs"]

    async def _noisy_aclose() -> None:
        raise RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )

    rec.stack.aclose = _noisy_aclose  # type: ignore[method-assign]
    await client.disconnect("fs")
    assert client.all_tools() == []


@pytest.mark.asyncio
async def test_load_from_config_streamable_http_url(tmp_path: Path) -> None:
    """``url`` entries call :meth:`MCPClient.connect_streamable_http` (no subprocess)."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {"url": "https://docs.langchain.com/mcp", "headers": {}},
                },
            }
        ),
        encoding="utf-8",
    )

    client = MCPClient(hooks=_CrashHooks())
    with patch.object(client, "connect_streamable_http", new_callable=AsyncMock) as m:
        m.return_value = []
        await client.load_from_config(cfg)
    m.assert_awaited_once_with("docs", "https://docs.langchain.com/mcp", None)


@pytest.mark.asyncio
async def test_load_from_config_skips_enabled_false_servers(tmp_path: Path) -> None:
    """Servers with ``enabled: false`` are ignored (template examples stay inert)."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "off": {
                        "enabled": False,
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                        "env": {"FOO": "1"},
                    },
                    "on": {"command": "python", "args": ["x.py"], "env": {}},
                },
            }
        ),
        encoding="utf-8",
    )

    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.load_from_config(cfg)
    assert "off" not in client._servers
    assert "on" in client._servers


@pytest.mark.asyncio
async def test_load_from_config_missing_file_noop(tmp_path: Path) -> None:
    """Missing config paths do nothing."""
    bogus = tmp_path / "no_such.json"

    client = MCPClient(hooks=_CrashHooks())
    await client.load_from_config(bogus)
    assert client.all_tools() == []


@pytest.mark.asyncio
async def test_load_from_config_logs_and_continues_on_one_bad_server(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One rotten server yields ERROR logs yet later servers stay connected."""

    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "first": {"command": "python", "args": ["first.py"], "env": {}},
                    "good": {"command": "python", "args": ["good.py"], "env": {}},
                },
            }
        ),
        encoding="utf-8",
    )

    good_sess = SimpleNamespace()
    good_sess.initialize = AsyncMock()
    good_sess.list_tools = AsyncMock(
        return_value=SimpleNamespace(tools=[SimpleNamespace(name="noop", description="x")])
    )

    hooked = MCPClient(hooks=_stub_hooks(good_sess))
    base_connect = MCPClient.connect

    async def patched_connect(
        self: MCPClient,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> object:
        if name == "first":
            raise MCPConnectionError(name)
        return await base_connect(self, name, command, args, env)

    hooked.connect = MethodType(patched_connect, hooked)  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="monkeybot.core.mcp.mcp_client"):
        await hooked.load_from_config(cfg)

    assert len(caplog.records) >= 1
    msgs = [(r.msg % r.args if r.args else r.msg) + (r.exc_text or "") for r in caplog.records]
    merged = "\n".join(str(m) for m in msgs).lower()
    assert "startup connect failed for server first" in merged
    tool_names = [t.name for t in hooked.all_tools()]
    assert tool_names == ["good__noop"]


@pytest.mark.asyncio
async def test_connect_replaces_existing_server() -> None:
    """Reconnecting replaces prior tools and exits old stacks cleanly."""
    sess = SimpleNamespace()
    sess.initialize = AsyncMock()

    sess.list_tools = AsyncMock(
        side_effect=[
            SimpleNamespace(
                tools=[SimpleNamespace(name="A", description="a", inputSchema={})]
            ),
            SimpleNamespace(
                tools=[SimpleNamespace(name="B", description="b", inputSchema={})]
            ),
        ]
    )

    client = MCPClient(hooks=_stub_hooks(sess))

    await client.connect("fs", "python", [], {})
    tools_a = {t.name for t in client.all_tools()}
    assert tools_a == {"fs__A"}

    await client.connect("fs", "python", [], {})
    tools_b = {t.name for t in client.all_tools()}
    assert tools_b == {"fs__B"}


def test_split_prefixed_tool_longest_server_wins() -> None:
    """``ab__t`` must match server ``ab``, not ``a``."""
    client = MCPClient(hooks=_CrashHooks())
    client._servers["a"] = object()  # type: ignore[attr-defined]
    client._servers["ab"] = object()  # type: ignore[attr-defined]
    assert client.split_prefixed_tool("ab__tool") == ("ab", "tool")
    assert client.split_prefixed_tool("a__x") == ("a", "x")


def test_split_prefixed_tool_unknown_returns_none() -> None:
    client = MCPClient(hooks=_CrashHooks())
    assert client.split_prefixed_tool("nope__x") is None
