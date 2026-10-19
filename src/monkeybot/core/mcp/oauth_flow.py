"""Browser OAuth for remote MCP servers (authorization code + PKCE).

This is the flow Cursor uses for an MCP URL: the server answers 401, we
discover its OAuth metadata, open the system browser, and keep the tokens so
later connects can present them. Tokens never go in ``mcp.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from monkeybot.core.logging_utils import kv
from monkeybot.core.mcp.mcp_client import MCPAuthError

logger = logging.getLogger(__name__)

_ENV_DIR = "MONKEYBOT_MCP_OAUTH_DIR"
_LOGIN_TIMEOUT_S = 180.0
_PROBE_TIMEOUT_S = 8.0
_LEGACY_GRANTS = frozenset({"client_credentials", "password"})

_PAGE_OK = b"<html><body><p>You can close this window and return to Spaces.</p></body></html>"
_PAGE_FAIL = (
    b"<html><body><p>Sign-in did not finish. Return to Spaces and try again.</p></body></html>"
)


def oauth_storage_dir() -> Path | None:
    """Directory the app set for MCP OAuth tokens, or None when unset."""
    raw = (os.environ.get(_ENV_DIR) or "").strip()
    return Path(raw) if raw else None


def token_filename(url: str) -> str:
    """Stable file name for one server URL. The URL itself is not the name."""
    digest = hashlib.sha256(url.strip().encode()).hexdigest()[:32]
    return f"{digest}.json"


def server_uses_browser_oauth(spec: dict[str, Any]) -> bool:
    """True when a remote URL should sign in through the browser.

    An explicit client-credentials or password grant stays on the legacy form.
    An ``Authorization`` header is already a credential, so it is left alone.
    """
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        return False
    headers = spec.get("headers")
    if isinstance(headers, dict):
        for key in headers:
            if str(key).lower() == "authorization" and str(headers[key]).strip():
                return False
    auth = spec.get("auth")
    if isinstance(auth, dict):
        flow = str(auth.get("flow") or auth.get("grant_type") or "").strip().lower()
        if flow in _LEGACY_GRANTS:
            return False
    return True


def find_auth_error(exc: BaseException) -> MCPAuthError | None:
    """Walk a wrapped failure (including ExceptionGroup) for ``MCPAuthError``."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, MCPAuthError):
            return current
        cause = current.__cause__ or current.__context__
        if cause is not None:
            stack.append(cause)
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            stack.extend(nested)
    return None


class FileTokenStorage:
    """One JSON file per server URL, readable only by the current user."""

    def __init__(self, directory: Path, url: str) -> None:
        self.directory = directory
        self.url = url.strip()
        self.path = directory / token_filename(self.url)

    def has_access_token(self) -> bool:
        """True when a previous sign-in left an access token on disk."""
        data = self._read()
        tokens = data.get("tokens") if isinstance(data, dict) else None
        return isinstance(tokens, dict) and bool(str(tokens.get("access_token") or "").strip())

    async def get_tokens(self) -> Any | None:
        """Load ``OAuthToken`` or return None when this server has never signed in."""
        from mcp.shared.auth import OAuthToken

        data = self._read()
        raw = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or not raw.get("access_token"):
            return None
        return OAuthToken.model_validate(raw)

    async def set_tokens(self, tokens: Any) -> None:
        """Persist tokens without dropping the client registration beside them."""
        data = self._read()
        data["server_url"] = self.url
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> Any | None:
        """Load the dynamic-client registration, if one was stored."""
        from mcp.shared.auth import OAuthClientInformationFull

        data = self._read()
        raw = data.get("client_info") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or not raw.get("client_id"):
            return None
        return OAuthClientInformationFull.model_validate(raw)

    async def set_client_info(self, client_info: Any) -> None:
        """Persist the client registration returned by the authorization server."""
        data = self._read()
        data["server_url"] = self.url
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)

    def drop_client_if_redirect_mismatch(self, redirect_uri: str) -> None:
        """Forget a registration whose callback URL is a different loopback port.

        The SDK only registers a client when none is stored. A previous login
        pinned the old port, so a new login would send the browser to a port
        that is no longer listening.
        """
        data = self._read()
        raw = data.get("client_info")
        if not isinstance(raw, dict):
            return
        redirects = raw.get("redirect_uris") or []
        if redirect_uri in {str(item) for item in redirects}:
            return
        data["client_info"] = None
        data["tokens"] = None
        self._write(data)

    def remember_refresh_target(self, *, token_endpoint: str | None, resource: str | None) -> None:
        """Remember where a later connect can refresh this server's token."""
        data = self._read()
        if token_endpoint:
            data["token_endpoint"] = token_endpoint
        if resource:
            data["resource"] = resource
        data["server_url"] = self.url
        self._write(data)

    async def refresh_access_token(self) -> str | None:
        """Exchange a stored refresh token.

        Returns the new access token, or None when nothing is stored to refresh.
        A failed exchange raises ``MCPAuthError``.
        """
        data = self._read()
        raw_tokens = data.get("tokens")
        raw_client = data.get("client_info")
        tokens: dict[str, Any] = raw_tokens if isinstance(raw_tokens, dict) else {}
        client: dict[str, Any] = raw_client if isinstance(raw_client, dict) else {}
        refresh = str(tokens.get("refresh_token") or "").strip()
        endpoint = str(data.get("token_endpoint") or "").strip()
        client_id = str(client.get("client_id") or "").strip()
        if not refresh or not endpoint or not client_id:
            return None
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
        secret = str(client.get("client_secret") or "").strip()
        if secret:
            form["client_secret"] = secret
        resource = str(data.get("resource") or "").strip()
        if resource:
            form["resource"] = resource
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
                response = await http.post(
                    endpoint,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise self._refresh_failed(error=type(exc).__name__) from exc
        if response.status_code != 200:
            raise self._refresh_failed(status=response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._refresh_failed(error="invalid_json") from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise self._refresh_failed(error="missing_access_token")
        merged = dict(tokens)
        merged.update(payload)
        if "refresh_token" not in payload and refresh:
            merged["refresh_token"] = refresh
        data["tokens"] = merged
        self._write(data)
        return str(payload["access_token"])

    def _refresh_failed(self, **fields: object) -> MCPAuthError:
        logger.warning("mcp oauth refresh failed %s", kv(host=_host_of(self.url), **fields))
        return MCPAuthError(
            "mcp",
            "This tool needs you to sign in.",
            remedy="Use Sign in on the tool in Studio.",
        )

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"server_url": self.url}
        except json.JSONDecodeError as exc:
            logger.error("mcp oauth token file is corrupt %s", kv(host=_host_of(self.url)))
            raise MCPAuthError(
                "mcp",
                "Saved sign-in data for this tool is unreadable.",
                remedy="Sign out and sign in again.",
            ) from exc
        except OSError as exc:
            logger.error(
                "mcp oauth token file could not be read %s",
                kv(host=_host_of(self.url), error=type(exc).__name__),
            )
            raise
        if not isinstance(raw, dict):
            logger.error("mcp oauth token file is corrupt %s", kv(host=_host_of(self.url)))
            raise MCPAuthError(
                "mcp",
                "Saved sign-in data for this tool is unreadable.",
                remedy="Sign out and sign in again.",
            )
        return raw

    def _write(self, data: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            self.directory.chmod(0o700)
        payload = json.dumps(data, indent=2) + "\n"
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
        tmp.replace(self.path)


class StoredBearerAuth(httpx.Auth):
    """Attach a stored access token, and refresh it once on 401.

    A refresh that fails raises ``MCPAuthError`` so the connect is marked
    needs-auth. This path never opens a browser and never registers a client.
    """

    requires_response_body = False

    def __init__(self, storage: FileTokenStorage) -> None:
        self.storage = storage

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        tokens = await self.storage.get_tokens()
        if tokens is not None and tokens.access_token:
            request.headers["Authorization"] = f"Bearer {tokens.access_token}"
        response = yield request
        if response.status_code != 401:
            return
        refreshed = await self.storage.refresh_access_token()
        if not refreshed:
            raise MCPAuthError(
                "mcp",
                "This tool needs you to sign in.",
                remedy="Use Sign in on the tool in Studio.",
            )
        request.headers["Authorization"] = f"Bearer {refreshed}"
        yield request


def oauth_auth_for_connect(url: str) -> StoredBearerAuth | None:
    """Bearer auth when this URL already has tokens. Otherwise None.

    Unsigned servers stay unauthenticated so a background connect does not
    register an OAuth client or wait on a browser.
    """
    directory = oauth_storage_dir()
    if directory is None:
        return None
    storage = FileTokenStorage(directory, url)
    if not storage.has_access_token():
        return None
    return StoredBearerAuth(storage)


async def looks_like_oauth_challenge(url: str) -> bool:
    """True when a short unauthenticated probe is rejected as OAuth.

    Used only after a connect that had no stored token already failed, so a
    healthy server is not probed on every enable.
    """
    headers, body = _initialize_call()
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=False) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError:
        return False
    if response.status_code != 401:
        return False
    challenge = response.headers.get("www-authenticate", "")
    lowered = challenge.lower()
    if "bearer" in lowered or "resource_metadata" in lowered:
        return True
    # A 401 with no challenge still often means OAuth. Confirm the metadata URL.
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    metadata = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
            meta = await client.get(metadata)
    except httpx.HTTPError:
        return False
    return meta.status_code == 200


class _LoopbackCallback:
    """One-shot ``127.0.0.1`` listener for the OAuth redirect."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._future: asyncio.Future[tuple[str, str | None]] | None = None

    async def start(self) -> str:
        """Bind a free port and return the redirect URI the browser will hit."""
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets or ()), None)
        if sock is None:
            raise MCPAuthError("mcp", "Could not listen for the sign-in redirect.")
        port = sock.getsockname()[1]
        return f"http://127.0.0.1:{port}/callback"

    async def wait(self) -> tuple[str, str | None]:
        """Block until the browser hits the callback, or the login times out."""
        if self._future is None:
            raise MCPAuthError("mcp", "Sign-in did not start.")
        try:
            return await asyncio.wait_for(self._future, timeout=_LOGIN_TIMEOUT_S)
        except TimeoutError as exc:
            raise MCPAuthError(
                "mcp",
                "Sign-in timed out before the browser finished.",
                remedy="Try Sign in again.",
            ) from exc

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        page = _PAGE_FAIL
        try:
            raw = await asyncio.wait_for(reader.read(8192), timeout=5)
            head = raw.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            target = head.split(" ")[1] if head.startswith("GET ") and " " in head else "/"
            query = parse_qs(urlparse(target).query)
            code = (query.get("code") or [""])[0]
            state_values = query.get("state")
            state = state_values[0] if state_values else None
            error = (query.get("error") or [""])[0]
            if code and not error and self._future is not None and not self._future.done():
                page = _PAGE_OK
                self._future.set_result((code, state))
            elif self._future is not None and not self._future.done():
                self._future.set_exception(
                    MCPAuthError("mcp", error or "The sign-in redirect had no authorization code.")
                )
        except Exception as exc:  # noqa: BLE001 — the browser still needs a page
            if self._future is not None and not self._future.done():
                self._future.set_exception(exc)
        finally:
            body = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Connection: close\r\n\r\n" + page
            )
            writer.write(body)
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload), flush=True)


def _oauth_flow_message(exc: BaseException) -> str | None:
    """A short sign-in failure from the MCP SDK, walking wrapped causes."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        name = current.__class__.__name__
        if name in {"OAuthRegistrationError", "OAuthFlowError"}:
            if name == "OAuthRegistrationError" or "Registration failed" in str(current):
                return (
                    "This server will not register an app automatically. "
                    "Add its OAuth client ID on the tool, then try Sign in again."
                )
            text = str(current).strip()
            return text or "Sign-in did not finish."
        cause = current.__cause__ or current.__context__
        if cause is not None:
            stack.append(cause)
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            stack.extend(item for item in nested if isinstance(item, BaseException))
    return None


def _quiet_sdk_logs() -> None:
    """Keep the MCP SDK from writing a traceback to stderr during sign-in."""
    logging.getLogger("mcp").setLevel(logging.CRITICAL)


async def remember_preregistered_client(
    storage: FileTokenStorage,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None = None,
) -> None:
    """Store a client the user already registered with this server.

    The browser flow then skips dynamic registration and uses this client.
    A different client id drops any tokens from the previous one.
    """
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    secret = (client_secret or "").strip() or None
    current = await storage.get_client_info()
    if current is None or current.client_id != client_id:
        data = storage._read()
        data["tokens"] = None
        storage._write(data)
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id=client_id,
            client_secret=secret,
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if secret else "none",
            client_name="Spaces",
        )
    )


def _initialize_call() -> tuple[dict[str, str], dict[str, Any]]:
    """Headers and body for one unauthenticated MCP initialize."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "spaces", "version": "0"},
        },
    }
    return headers, body


def _client_metadata(redirect_uri: str) -> Any:
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    return OAuthClientMetadata(
        redirect_uris=[AnyUrl(redirect_uri)],
        client_name="Spaces",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


async def login_mcp_server(
    url: str,
    storage_dir: Path,
    *,
    open_browser: bool = False,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Run one interactive OAuth login. Prints ``{"url": ...}`` when the browser should open.

    Returns ``{"ok": True, "signed_in": True}`` after tokens are stored, or
    ``signed_in`` false when the server accepted the connection without OAuth.
    ``open_browser`` opens the system browser from this process. The app passes
    false and opens the URL itself.
    """
    from mcp.client.auth import OAuthClientProvider

    _quiet_sdk_logs()
    cleaned = url.strip()
    if not cleaned:
        raise MCPAuthError("mcp", "This tool has no URL to sign in to.")
    storage = FileTokenStorage(storage_dir, cleaned)
    supplied_client_id = (client_id or "").strip()
    loopback = _LoopbackCallback()
    try:
        redirect_uri = await loopback.start()
        if supplied_client_id:
            await remember_preregistered_client(
                storage,
                redirect_uri,
                supplied_client_id,
                client_secret,
            )
        else:
            storage.drop_client_if_redirect_mismatch(redirect_uri)
        provider = OAuthClientProvider(
            server_url=cleaned,
            client_metadata=_client_metadata(redirect_uri),
            storage=storage,
            redirect_handler=lambda auth_url: _open_authorization(auth_url, open_browser),
            callback_handler=loopback.wait,
            timeout=_LOGIN_TIMEOUT_S,
        )
        response = await _post_initialize(cleaned, provider)
        return await _login_result(storage, provider, response, cleaned)
    finally:
        await loopback.close()


async def _open_authorization(auth_url: str, open_browser: bool) -> None:
    _emit({"url": auth_url})
    if not open_browser:
        return
    import webbrowser

    webbrowser.open(auth_url)


async def _post_initialize(url: str, provider: Any) -> httpx.Response:
    """POST initialize and stay open while the user finishes the browser step."""
    headers, body = _initialize_call()
    try:
        async with httpx.AsyncClient(
            auth=provider,
            timeout=httpx.Timeout(_LOGIN_TIMEOUT_S + 15, connect=20.0),
            follow_redirects=False,
        ) as client:
            return await client.post(url, json=body, headers=headers)
    except Exception as exc:
        message = _oauth_flow_message(exc)
        if message:
            raise MCPAuthError("mcp", message) from exc
        raise


async def _login_result(
    storage: FileTokenStorage,
    provider: Any,
    response: httpx.Response,
    url: str,
) -> dict[str, Any]:
    if response.status_code == 401:
        raise MCPAuthError(
            "mcp",
            "Sign-in did not grant access to this tool.",
            remedy="Try Sign in again.",
        )
    tokens = await storage.get_tokens()
    signed_in = tokens is not None and bool(tokens.access_token)
    if signed_in:
        meta = provider.context.oauth_metadata
        endpoint = str(meta.token_endpoint) if meta is not None else None
        storage.remember_refresh_target(
            token_endpoint=endpoint,
            resource=provider.context.get_resource_url(),
        )
    logger.info(
        "mcp oauth sign-in finished %s",
        kv(host=_host_of(url), signed_in=signed_in),
    )
    return {"ok": True, "signed_in": signed_in}


def _host_of(url: str) -> str:
    host = urlparse(url).netloc
    return host or "unknown"
