"""Regression tests for browser OAuth failure paths. No live authorization server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import socket
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from monkeybot.core.mcp import oauth_flow
from monkeybot.core.mcp.mcp_client import MCP_STATUS_NEEDS_AUTH, MCPAuthError, MCPClient
from monkeybot.core.mcp.oauth_flow import (
    FileTokenStorage,
    StoredBearerAuth,
    _LoopbackCallback,
    login_mcp_server,
    looks_like_oauth_challenge,
    token_filename,
)

MCP_URL = "https://mcp.example.com/mcp"
TOKEN_URL = "https://auth.example.com/token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _route_async_clients(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Send every ``httpx.AsyncClient`` built inside oauth_flow through ``handler``."""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _seed(
    storage: FileTokenStorage,
    *,
    access: str = "old",
    refresh: str | None = "refresh-1",
    client_id: str = "client",
    redirect_uri: str = "http://127.0.0.1:9/callback",
    client_secret: str | None = None,
    auth_method: str | None = None,
) -> None:
    await storage.set_tokens(OAuthToken(access_token=access, refresh_token=refresh))
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[AnyUrl(redirect_uri)],
            token_endpoint_auth_method=auth_method,
        )
    )
    storage.remember_refresh_target(token_endpoint=TOKEN_URL, resource=None)


def _write_config(tmp_path, servers: dict) -> object:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return config


async def test_corrupt_token_file_does_not_stop_other_servers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oauth_dir = tmp_path / "oauth"
    oauth_dir.mkdir()
    monkeypatch.setenv("MONKEYBOT_MCP_OAUTH_DIR", str(oauth_dir))
    bad_url = "https://bad.example.com/mcp"
    (oauth_dir / token_filename(bad_url)).write_text("{", encoding="utf-8")
    config = _write_config(
        tmp_path,
        {
            "alpha": {"url": bad_url, "autoConnect": True},
            "beta": {"url": "https://good.example.com/mcp", "autoConnect": True},
        },
    )
    connected: list[str] = []

    async def fake_connect(self, name, url, headers=None, *, auth=None):
        connected.append(name)
        return []

    monkeypatch.setattr(MCPClient, "connect_streamable_http", fake_connect)
    client = MCPClient()
    await client.load_from_config(config)
    assert connected == ["beta"]
    status = client.status("alpha")
    assert status["status"] == MCP_STATUS_NEEDS_AUTH
    assert "unreadable" in status["error"]


async def test_connect_routes_stored_tokens_and_legacy_grants(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oauth_dir = tmp_path / "oauth"
    monkeypatch.setenv("MONKEYBOT_MCP_OAUTH_DIR", str(oauth_dir))
    await FileTokenStorage(oauth_dir, MCP_URL).set_tokens(OAuthToken(access_token="access"))
    config = _write_config(
        tmp_path,
        {
            "signed": {"url": MCP_URL, "autoConnect": True},
            "legacy": {
                "url": "https://legacy.example.com/mcp",
                "autoConnect": True,
                "auth": {"flow": "client_credentials", "token_url": TOKEN_URL},
            },
        },
    )
    seen: dict[str, object] = {}

    async def fake_connect(self, name, url, headers=None, *, auth=None):
        seen[name] = auth
        return []

    monkeypatch.setattr(MCPClient, "connect_streamable_http", fake_connect)
    await MCPClient().load_from_config(config)
    assert isinstance(seen["signed"], StoredBearerAuth)
    assert type(seen["legacy"]).__name__ == "MCPAuthHandler"


async def test_auth_error_names_the_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.client.streamable_http as streamable_http

    @asynccontextmanager
    async def rejected(url, http_client=None, **kwargs):
        raise BaseExceptionGroup(
            "transport",
            [MCPAuthError("mcp", "This tool needs you to sign in.", remedy="Sign in.")],
        )
        yield

    monkeypatch.setattr(streamable_http, "streamable_http_client", rejected)
    client = MCPClient()
    with pytest.raises(MCPAuthError) as info:
        await client.connect_streamable_http("notion", MCP_URL)
    assert info.value.server_name == "notion"
    assert info.value.remedy == "Sign in."
    assert client.status("notion")["status"] == MCP_STATUS_NEEDS_AUTH


@pytest.mark.parametrize("client_id", [None, "new-client"])
async def test_cancelled_login_keeps_the_previous_sign_in(
    tmp_path, monkeypatch: pytest.MonkeyPatch, client_id: str | None
) -> None:
    storage = FileTokenStorage(tmp_path, MCP_URL)
    await _seed(storage, access="working", client_id="old-client")

    async def cancelled(url, provider):
        raise MCPAuthError("mcp", "Sign-in timed out before the browser finished.")

    monkeypatch.setattr(oauth_flow, "_post_initialize", cancelled)
    with pytest.raises(MCPAuthError, match="timed out"):
        await login_mcp_server(MCP_URL, tmp_path, client_id=client_id)
    tokens = await storage.get_tokens()
    assert tokens is not None and tokens.access_token == "working"
    info = await storage.get_client_info()
    assert info is not None and info.client_id == "old-client"


async def test_login_reuses_the_stored_redirect_port(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redirect = f"http://127.0.0.1:{_free_port()}/callback"
    await _seed(FileTokenStorage(tmp_path, MCP_URL), redirect_uri=redirect)
    seen: list[str] = []
    real_metadata = oauth_flow._client_metadata

    def spy(redirect_uri: str):
        seen.append(redirect_uri)
        return real_metadata(redirect_uri)

    async def stop(url, provider):
        raise MCPAuthError("mcp", "stop")

    monkeypatch.setattr(oauth_flow, "_client_metadata", spy)
    monkeypatch.setattr(oauth_flow, "_post_initialize", stop)
    with pytest.raises(MCPAuthError):
        await login_mcp_server(MCP_URL, tmp_path)
    assert seen == [redirect]


async def test_login_binds_an_explicit_redirect_port(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    seen: list[str] = []
    real_metadata = oauth_flow._client_metadata

    def spy(redirect_uri: str):
        seen.append(redirect_uri)
        return real_metadata(redirect_uri)

    async def stop(url, provider):
        raise MCPAuthError("mcp", "stop")

    monkeypatch.setattr(oauth_flow, "_client_metadata", spy)
    monkeypatch.setattr(oauth_flow, "_post_initialize", stop)
    with pytest.raises(MCPAuthError):
        await login_mcp_server(MCP_URL, tmp_path, redirect_port=port)
    assert seen == [f"http://127.0.0.1:{port}/callback"]


async def test_login_restores_the_sdk_log_level(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sdk_logger = logging.getLogger("mcp")
    previous = sdk_logger.level
    sdk_logger.setLevel(logging.INFO)

    async def stop(url, provider):
        raise MCPAuthError("mcp", "stop")

    monkeypatch.setattr(oauth_flow, "_post_initialize", stop)
    try:
        with pytest.raises(MCPAuthError):
            await login_mcp_server(MCP_URL, tmp_path)
        assert sdk_logger.level == logging.INFO
    finally:
        sdk_logger.setLevel(previous)


async def test_concurrent_401s_share_one_refresh(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed(FileTokenStorage(tmp_path, MCP_URL))
    state = {"access": "valid-only-after-refresh", "refresh": "refresh-1", "rotations": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            sent = parse_qs(request.content.decode())["refresh_token"][0]
            await asyncio.sleep(0.05)
            if sent != state["refresh"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            state["rotations"] += 1
            state["access"] = f"access-{state['rotations'] + 1}"
            state["refresh"] = f"refresh-{state['rotations'] + 1}"
            return httpx.Response(
                200, json={"access_token": state["access"], "refresh_token": state["refresh"]}
            )
        if request.headers.get("authorization") != f"Bearer {state['access']}":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    _route_async_clients(monkeypatch, handler)

    async def call() -> int:
        auth = StoredBearerAuth(FileTokenStorage(tmp_path, MCP_URL))
        async with httpx.AsyncClient(auth=auth) as client:
            return (await client.get(MCP_URL)).status_code

    assert await asyncio.gather(call(), call()) == [200, 200]
    assert state["rotations"] == 1


async def test_retry_that_is_still_401_is_needs_auth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(FileTokenStorage(tmp_path, MCP_URL))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "new", "token_type": "Bearer"})
        return httpx.Response(401)

    _route_async_clients(monkeypatch, handler)
    auth = StoredBearerAuth(FileTokenStorage(tmp_path, MCP_URL))
    async with httpx.AsyncClient(auth=auth) as client:
        with pytest.raises(MCPAuthError, match="sign in"):
            await client.get(MCP_URL)


async def test_refresh_does_not_follow_redirects(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FileTokenStorage(tmp_path, MCP_URL)
    await _seed(storage)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(307, headers={"Location": "https://evil.example/steal"})
        return httpx.Response(200, json={"access_token": "stolen"})

    _route_async_clients(monkeypatch, handler)
    with pytest.raises(MCPAuthError):
        await storage.refresh_access_token()
    assert "evil.example" not in hosts


async def test_refresh_uses_basic_auth_when_registered(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FileTokenStorage(tmp_path, MCP_URL)
    await _seed(storage, client_secret="secret", auth_method="client_secret_basic")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "new", "token_type": "Bearer"})

    _route_async_clients(monkeypatch, handler)
    assert await storage.refresh_access_token() == "new"
    expected = base64.b64encode(b"client:secret").decode()
    assert requests[0].headers["authorization"] == f"Basic {expected}"
    assert "client_secret" not in requests[0].content.decode()


async def test_loopback_ignores_requests_to_other_paths() -> None:
    loopback = _LoopbackCallback()
    redirect = await loopback.start()
    try:
        async with httpx.AsyncClient() as client:
            stray = await client.get(redirect.replace("/callback", "/favicon.ico"))
            assert stray.status_code == 404
            await client.get(redirect, params={"code": "abc", "state": "xyz"})
        assert await loopback.wait() == ("abc", "xyz")
    finally:
        await loopback.close()


async def test_challenge_probe_checks_path_specific_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(401)
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": MCP_URL})
        return httpx.Response(404)

    _route_async_clients(monkeypatch, handler)
    assert await looks_like_oauth_challenge(MCP_URL) is True


def test_mcp_login_reads_the_client_secret_from_the_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot_cli.commands import mcp_login

    captured: dict[str, object] = {}

    async def fake_login(url, storage, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "signed_in": True}

    monkeypatch.setattr(mcp_login, "login_mcp_server", fake_login)
    monkeypatch.setenv("MONKEYBOT_MCP_CLIENT_SECRET", "from-env")
    parser = argparse.ArgumentParser()
    mcp_login.register(parser.add_subparsers())
    args = parser.parse_args(
        ["mcp-login", "--url", MCP_URL, "--storage", str(tmp_path), "--client-id", "client"]
    )
    assert args.func(args) == 0
    assert captured["client_secret"] == "from-env"
