"""Browser OAuth token storage and the sign-in callback. No live authorization server."""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from monkeybot.core.mcp.mcp_client import MCPAuthError
from monkeybot.core.mcp.oauth_flow import (
    FileTokenStorage,
    StoredBearerAuth,
    _LoopbackCallback,
    _oauth_flow_message,
    _stage_login,
    server_uses_browser_oauth,
    token_filename,
)


def test_missing_token_file_is_unsigned(tmp_path) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    assert storage.has_access_token() is False


def test_token_filename_is_stable_and_hides_the_url() -> None:
    name = token_filename("https://mcp.example.com/mcp")
    assert name == token_filename("https://mcp.example.com/mcp")
    assert name.endswith(".json")
    assert "mcp.example" not in name


def test_server_uses_browser_oauth_skips_legacy_grants() -> None:
    assert server_uses_browser_oauth({"url": "https://mcp.example.com/mcp"})
    assert not server_uses_browser_oauth({"command": "npx"})
    assert not server_uses_browser_oauth(
        {
            "url": "https://mcp.example.com/mcp",
            "auth": {"flow": "client_credentials", "token_url": "https://auth.example/token"},
        }
    )
    assert not server_uses_browser_oauth(
        {
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer already"},
        }
    )
    assert server_uses_browser_oauth(
        {"url": "https://mcp.example.com/mcp", "headers": {"X-Trace": "1"}}
    )


async def test_storage_roundtrip(tmp_path) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    await storage.set_tokens(OAuthToken(access_token="access", refresh_token="refresh"))
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="client",
            redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
        )
    )
    loaded = await storage.get_tokens()
    assert loaded is not None
    assert loaded.access_token == "access"
    info = await storage.get_client_info()
    assert info is not None
    assert info.client_id == "client"
    assert storage.has_access_token()
    mode = stat.S_IMODE(storage.path.stat().st_mode)
    assert mode == 0o600


async def test_login_on_a_new_port_registers_a_fresh_client(tmp_path) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    await storage.set_tokens(OAuthToken(access_token="access"))
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="client",
            redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
        )
    )
    stored = await storage.get_client_info()

    same_port = _stage_login(storage, stored, "http://127.0.0.1:9/callback", "", None)
    assert (await same_port.get_client_info()).client_id == "client"
    assert (await same_port.get_tokens()).access_token == "access"

    new_port = _stage_login(storage, stored, "http://127.0.0.1:10/callback", "", None)
    assert await new_port.get_client_info() is None
    assert await new_port.get_tokens() is None
    assert (await storage.get_tokens()).access_token == "access"


async def test_refresh_keeps_the_old_refresh_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    await storage.set_tokens(OAuthToken(access_token="old", refresh_token="refresh-1"))
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="client",
            redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
        )
    )
    storage.remember_refresh_target(
        token_endpoint="https://auth.example/token",
        resource="https://mcp.example.com/mcp",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh-1" in body
        return httpx.Response(200, json={"access_token": "new", "token_type": "Bearer"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    refreshed = await storage.refresh_access_token()
    assert refreshed == "new"
    saved = json.loads(storage.path.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == "new"
    assert saved["tokens"]["refresh_token"] == "refresh-1"


async def test_refresh_failure_raises_and_keeps_the_saved_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    await storage.set_tokens(OAuthToken(access_token="old", refresh_token="refresh-1"))
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="client",
            redirect_uris=[AnyUrl("http://127.0.0.1:9/callback")],
        )
    )
    storage.remember_refresh_target(token_endpoint="https://auth.example/token", resource=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    with pytest.raises(MCPAuthError, match="sign in"):
        await storage.refresh_access_token()
    saved = json.loads(storage.path.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == "old"


def test_corrupt_token_file_raises_and_is_left_in_place(tmp_path) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    storage.path.write_text("{", encoding="utf-8")
    with pytest.raises(MCPAuthError, match="unreadable"):
        storage.has_access_token()
    assert storage.path.read_text(encoding="utf-8") == "{"


async def test_stored_bearer_sends_the_access_token(tmp_path) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    await storage.set_tokens(OAuthToken(access_token="access"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access"
        return httpx.Response(200, json={"ok": True})

    auth = StoredBearerAuth(storage)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        response = await client.get("https://mcp.example.com/mcp")
    assert response.status_code == 200


async def test_loopback_callback_returns_the_code() -> None:
    loopback = _LoopbackCallback()
    redirect = await loopback.start()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(redirect, params={"code": "abc", "state": "xyz"})
        assert response.status_code == 200
        assert "close this window" in response.text
        code, state = await loopback.wait()
    finally:
        await loopback.close()
    assert code == "abc"
    assert state == "xyz"


async def test_new_preregistered_client_does_not_reuse_old_tokens(tmp_path) -> None:
    storage = FileTokenStorage(tmp_path, "https://mcp.example.com/mcp")
    await storage.set_tokens(OAuthToken(access_token="old"))
    staged = _stage_login(storage, None, "http://127.0.0.1:9/callback", "client", "secret")
    info = await staged.get_client_info()
    assert info is not None
    assert info.client_id == "client"
    assert info.client_secret == "secret"
    assert info.token_endpoint_auth_method == "client_secret_post"
    assert await staged.get_tokens() is None
    assert (await storage.get_tokens()).access_token == "old"
    assert server_uses_browser_oauth(
        {"url": "https://mcp.example.com/mcp", "auth": {"client_id": "client"}}
    )


def test_registration_failure_is_a_short_message() -> None:
    class OAuthRegistrationError(Exception):
        pass

    message = _oauth_flow_message(OAuthRegistrationError("Registration failed: 404"))
    assert message is not None
    assert "client ID" in message
    assert "404" not in message
