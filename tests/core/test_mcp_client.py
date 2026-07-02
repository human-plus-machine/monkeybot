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
from monkeybot.core.mcp.mcp_client import (
    MCPAuthError,
    MCPClient,
    MCPConnectionError,
    _mcp_auth_handler_cls,
    interpolate_env_vars,
)


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
    from monkeybot.core.context.tool_output_policy import (
        resolve_tool_budget,
        reset_tool_output_policy_cache_for_tests,
    )

    reset_tool_output_policy_cache_for_tests()
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
    assert resolve_tool_budget("fs__read_file") is not None
    assert resolve_tool_budget("fs__write_file") is not None
    assert resolve_tool_budget("my__custom_tool") is None


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
        return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    )

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("fs", "python", [], {})
    resolved = await client.call_tool("fs", "read_file", {})

    assert resolved == "ok"
    sess.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_removes_tools() -> None:
    """Disconnected servers drop out of ``all_tools``; repeat disconnect is tolerated."""
    from monkeybot.core.context.tool_output_policy import (
        resolve_tool_budget,
        reset_tool_output_policy_cache_for_tests,
    )

    reset_tool_output_policy_cache_for_tests()
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
    assert resolve_tool_budget("fs__only") is not None
    await client.disconnect("fs")
    assert client.all_tools() == []
    assert resolve_tool_budget("fs__only") is None
    await client.disconnect("fs")
    assert client.all_tools() == []


@pytest.mark.asyncio
async def test_disconnect_calls_browser_stop_before_teardown() -> None:
    """A server exposing ``browser_stop`` gets it called before the transport closes.

    Regression test for H1: disconnecting a cloud-browser MCP server (e.g.
    browser-harness) must stop the remote session, not just kill the local
    stdio subprocess, or billing keeps accruing after the agent stops talking.
    """
    listing = SimpleNamespace(
        tools=[SimpleNamespace(name="browser_stop", description="stop", inputSchema={})]
    )
    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)
    sess.call_tool = AsyncMock(
        return_value=SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    )

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("browser", "python", [], {})
    await client.disconnect("browser")

    sess.call_tool.assert_awaited_once_with("browser_stop", arguments={})


@pytest.mark.asyncio
async def test_disconnect_skips_browser_stop_when_tool_absent() -> None:
    """Servers without a ``browser_stop`` tool are unaffected (no spurious call_tool)."""
    listing = SimpleNamespace(
        tools=[SimpleNamespace(name="read_file", description="d", inputSchema={})]
    )
    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)
    sess.call_tool = AsyncMock()

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("fs", "python", [], {})
    await client.disconnect("fs")

    sess.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_tolerates_browser_stop_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung/erroring ``browser_stop`` call is logged, not raised, and teardown proceeds."""
    listing = SimpleNamespace(
        tools=[SimpleNamespace(name="browser_stop", description="stop", inputSchema={})]
    )
    sess = SimpleNamespace()
    sess.initialize = AsyncMock()
    sess.list_tools = AsyncMock(return_value=listing)
    sess.call_tool = AsyncMock(side_effect=RuntimeError("remote unreachable"))

    client = MCPClient(hooks=_stub_hooks(sess))
    await client.connect("browser", "python", [], {})

    with caplog.at_level(logging.WARNING, logger="monkeybot.core.mcp.mcp_client"):
        await client.disconnect("browser")

    assert client.all_tools() == []
    assert any("browser_stop" in r.message for r in caplog.records)


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
    m.assert_awaited_once_with("docs", "https://docs.langchain.com/mcp", None, auth=None)


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
            SimpleNamespace(tools=[SimpleNamespace(name="A", description="a", inputSchema={})]),
            SimpleNamespace(tools=[SimpleNamespace(name="B", description="b", inputSchema={})]),
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


def test_interpolate_env_vars_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "abc123")
    assert interpolate_env_vars({"h": "Bearer ${MCP_TEST_TOKEN}"}) == {"h": "Bearer abc123"}
    assert interpolate_env_vars(["${MCP_TEST_TOKEN}", "x"]) == ["abc123", "x"]


@pytest.mark.asyncio
async def test_mcp_auth_client_credentials_fetches_token() -> None:
    from unittest.mock import patch

    import httpx

    posts: list[object] = []

    class _FakeTokenCM:
        async def __aenter__(self) -> _FakeTokenCM:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *a: object, **kw: object) -> httpx.Response:
            posts.append((a, kw))
            return httpx.Response(200, json={"access_token": "tok-z", "expires_in": 600})

    real_client = httpx.AsyncClient

    def _async_client_factory(**kw: object) -> object:
        timeout = kw.get("timeout")
        if isinstance(timeout, httpx.Timeout) and timeout.read == 600.0:
            return real_client(**kw)
        return _FakeTokenCM()

    with patch("httpx.AsyncClient", side_effect=_async_client_factory):
        cls = _mcp_auth_handler_cls()
        h = cls(
            "srv",
            {
                "flow": "client_credentials",
                "token_url": "https://issuer.example/oauth/token",
                "client_id": "cid",
                "client_secret": "sec",
            },
        )
        token = await h._get_token()

    assert token == "tok-z"
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_mcp_auth_async_auth_flow_retries_on_401() -> None:
    from unittest.mock import patch

    import httpx

    posts: list[int] = []

    class _FakeTokenCM:
        async def __aenter__(self) -> _FakeTokenCM:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *a: object, **kw: object) -> httpx.Response:
            posts.append(len(posts) + 1)
            if posts[-1] == 1:
                return httpx.Response(200, json={"access_token": "first", "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "second", "expires_in": 3600})

    real_client = httpx.AsyncClient

    def _async_client_factory(**kw: object) -> object:
        timeout = kw.get("timeout")
        if isinstance(timeout, httpx.Timeout) and timeout.read == 600.0:
            return real_client(**kw)
        return _FakeTokenCM()

    with patch("httpx.AsyncClient", side_effect=_async_client_factory):
        cls = _mcp_auth_handler_cls()
        h = cls(
            "srv",
            {
                "flow": "client_credentials",
                "token_url": "https://issuer.example/oauth/token",
                "client_id": "cid",
                "client_secret": "sec",
            },
        )
        req = httpx.Request("GET", "https://mcp.example/sse")
        gen = h.async_auth_flow(req)
        first = await anext(gen)
        assert first.headers.get("Authorization") == "Bearer first"
        resp = httpx.Response(401, request=first)
        second = await gen.asend(resp)
        assert second.headers.get("Authorization") == "Bearer second"

    assert len(posts) == 2


@pytest.mark.asyncio
async def test_load_from_config_raise_on_error_propagates_mcp_auth_error(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "url": "https://mcp.example/mcp",
                        "auth": {
                            "flow": "client_credentials",
                            "token_url": "https://issuer.example/oauth/token",
                            "client_id": "x",
                            "client_secret": "y",
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    client = MCPClient(hooks=_CrashHooks())

    async def _boom(
        *_a: object,
        **_kw: object,
    ) -> list[object]:
        raise MCPAuthError(
            "bad",
            "token failed",
            remedy="check credentials",
        )

    with patch.object(client, "connect_streamable_http", _boom), pytest.raises(MCPAuthError):
        await client.load_from_config(cfg, raise_on_error=True)


def test_interpolate_env_vars_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TEST_TOKEN", "def456")
    assert interpolate_env_vars({"h": "Bearer ${  MCP_TEST_TOKEN  }"}) == {"h": "Bearer def456"}


@pytest.mark.asyncio
async def test_mcp_auth_password_fetches_token() -> None:
    from unittest.mock import patch

    import httpx

    posts: list[object] = []

    class _FakeTokenCM:
        async def __aenter__(self) -> _FakeTokenCM:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *a: object, **kw: object) -> httpx.Response:
            posts.append((a, kw))
            return httpx.Response(200, json={"access_token": "pwd-tok", "expires_in": 1200})

    real_client = httpx.AsyncClient

    def _async_client_factory(**kw: object) -> object:
        timeout = kw.get("timeout")
        if isinstance(timeout, httpx.Timeout) and timeout.read == 600.0:
            return real_client(**kw)
        return _FakeTokenCM()

    with patch("httpx.AsyncClient", side_effect=_async_client_factory):
        cls = _mcp_auth_handler_cls()
        h = cls(
            "srv",
            {
                "flow": "password",
                "token_url": "https://issuer.example/oauth/token",
                "username": "bot-user",
                "password": "bot-password",
                "client_id": "cid",
                "client_secret": "sec",
            },
        )
        token = await h._get_token()

    assert token == "pwd-tok"
    assert len(posts) == 1
    data = posts[0][1]["data"]
    assert data["grant_type"] == "password"
    assert data["username"] == "bot-user"
    assert data["password"] == "bot-password"
    assert data["client_id"] == "cid"
    assert data["client_secret"] == "sec"


@pytest.mark.asyncio
async def test_mcp_auth_concurrent_stampede_prevention() -> None:
    from unittest.mock import patch

    import httpx

    refreshes = 0

    class _FakeTokenCM:
        async def __aenter__(self) -> _FakeTokenCM:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *a: object, **kw: object) -> httpx.Response:
            nonlocal refreshes
            refreshes += 1
            return httpx.Response(
                200, json={"access_token": f"tok-{refreshes}", "expires_in": 3600}
            )

    real_client = httpx.AsyncClient

    def _async_client_factory(**kw: object) -> object:
        timeout = kw.get("timeout")
        if isinstance(timeout, httpx.Timeout) and timeout.read == 600.0:
            return real_client(**kw)
        return _FakeTokenCM()

    with patch("httpx.AsyncClient", side_effect=_async_client_factory):
        cls = _mcp_auth_handler_cls()
        h = cls(
            "srv",
            {
                "flow": "client_credentials",
                "token_url": "https://issuer.example/oauth/token",
                "client_id": "cid",
                "client_secret": "sec",
            },
        )

        # Pre-prime with initial token
        token = await h._get_token()
        assert token == "tok-1"
        assert refreshes == 1

        # Simulate 3 concurrent tasks that already have request carrying tok-1 and receive 401
        import asyncio

        # Step 1: Initialize generators
        gens = [
            h.async_auth_flow(httpx.Request("GET", "https://mcp.example/sse")) for _ in range(3)
        ]

        # Step 2: Extract the initial request for all of them first.
        # Since self.access_token is tok-1 and no lock is held for refreshing,
        # all of them will successfully fetch tok-1 without blocking.
        p_reqs = []
        for g in gens:
            p_req = await anext(g)
            assert p_req.headers.get("Authorization") == "Bearer tok-1"
            p_reqs.append(p_req)

        # Step 3: Concurrently send the 401 response back to all three generators.
        # This forces them to concurrently compete for the lock to refresh the token.
        async def send_401(g: Any, p_req: httpx.Request) -> httpx.Request:
            resp = httpx.Response(401, request=p_req)
            retried_req = await g.asend(resp)
            return retried_req

        results = await asyncio.gather(*(send_401(g, p_req) for g, p_req in zip(gens, p_reqs)))

        # Under Double-Checked Locking, only 1 new refresh POST should be made,
        # and all three retried requests should have "Bearer tok-2" (not tok-3 or tok-4)
        assert refreshes == 2
        for r_req in results:
            assert r_req.headers.get("Authorization") == "Bearer tok-2"
