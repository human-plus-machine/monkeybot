"""Browser OAuth for remote MCP servers (authorization code + PKCE).

This is the flow Cursor uses for an MCP URL: the server answers 401, we
discover its OAuth metadata, open the system browser, and keep the tokens so
later connects can present them. Tokens never go in ``mcp.json``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from monkeybot.core.logging_utils import kv
from monkeybot.core.mcp.mcp_client import MCPAuthError

logger = logging.getLogger(__name__)

_ENV_DIR = "MONKEYBOT_MCP_OAUTH_DIR"
_LOGIN_TIMEOUT_S = 180.0
_PROBE_TIMEOUT_S = 8.0
_LEGACY_GRANTS = frozenset({"client_credentials", "password"})
_CALLBACK_PATH = "/callback"
_METADATA_PATH = "/.well-known/oauth-protected-resource"

# Keyed by event loop: an asyncio.Lock that has waited on one loop cannot be
# awaited on another.
_TOKEN_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)

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

    async def save_login(
        self,
        client_info: Any,
        tokens: Any,
        *,
        token_endpoint: str | None,
        resource: str | None,
    ) -> None:
        """Store a finished sign-in in one write. A corrupt file is replaced."""
        async with self._locked():
            try:
                data = self._read()
            except MCPAuthError:
                data = {}
            data["server_url"] = self.url
            data["client_info"] = client_info.model_dump(mode="json")
            data["tokens"] = tokens.model_dump(mode="json")
            if token_endpoint:
                data["token_endpoint"] = token_endpoint
            if resource:
                data["resource"] = resource
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

    async def refresh_access_token(self, stale_access_token: str | None = None) -> str | None:
        """Exchange a stored refresh token.

        Returns the new access token, or None when nothing is stored to refresh.
        When the stored access token no longer matches ``stale_access_token``,
        another caller already refreshed, and that token is returned instead.
        A failed exchange raises ``MCPAuthError``.
        """
        async with self._locked():
            return await self._refresh_locked(stale_access_token)

    async def _refresh_locked(self, stale_access_token: str | None) -> str | None:
        data = self._read()
        raw_tokens = data.get("tokens")
        raw_client = data.get("client_info")
        tokens: dict[str, Any] = raw_tokens if isinstance(raw_tokens, dict) else {}
        client: dict[str, Any] = raw_client if isinstance(raw_client, dict) else {}
        current = str(tokens.get("access_token") or "").strip()
        if stale_access_token and current and current != stale_access_token:
            return current
        refresh = str(tokens.get("refresh_token") or "").strip()
        endpoint = str(data.get("token_endpoint") or "").strip()
        client_id = str(client.get("client_id") or "").strip()
        if not refresh or not endpoint or not client_id:
            return None
        form = {"grant_type": "refresh_token", "refresh_token": refresh}
        request_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        secret = str(client.get("client_secret") or "").strip()
        if secret and client.get("token_endpoint_auth_method") == "client_secret_basic":
            # RFC 6749 section 2.3.1: form-encode the id and secret before Basic.
            credentials = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
            request_headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials.encode()).decode()
            )
        else:
            form["client_id"] = client_id
            if secret:
                form["client_secret"] = secret
        resource = str(data.get("resource") or "").strip()
        if resource:
            form["resource"] = resource
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
                response = await http.post(
                    endpoint,
                    data=form,
                    headers=request_headers,
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

    @contextlib.asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        """Serialize token changes for this server across tasks and processes."""
        locks = _TOKEN_LOCKS.setdefault(asyncio.get_running_loop(), {})
        async with locks.setdefault(self.path, asyncio.Lock()):
            self._ensure_directory()
            with self.path.with_suffix(".lock").open("a+b") as handle:
                await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            self.directory.chmod(0o700)

    def _write(self, data: dict[str, Any]) -> None:
        self._ensure_directory()
        payload = json.dumps(data, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            tmp.replace(self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


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
        sent = tokens.access_token if tokens is not None else None
        if sent:
            request.headers["Authorization"] = f"Bearer {sent}"
        response = yield request
        if response.status_code != 401:
            return
        refreshed = await self.storage.refresh_access_token(stale_access_token=sent)
        if not refreshed:
            raise _needs_sign_in()
        request.headers["Authorization"] = f"Bearer {refreshed}"
        retry = yield request
        if retry.status_code == 401:
            raise _needs_sign_in()


def _needs_sign_in() -> MCPAuthError:
    return MCPAuthError(
        "mcp",
        "This tool needs you to sign in.",
        remedy="Use Sign in on the tool in Studio.",
    )


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
    # A 401 with no challenge still often means OAuth. Confirm the metadata URL,
    # path-specific first (RFC 9728 section 3.1), then at the origin.
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [f"{origin}{_METADATA_PATH}"]
    path = parsed.path.rstrip("/")
    if path:
        candidates.insert(0, f"{origin}{_METADATA_PATH}{path}")
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S, follow_redirects=True) as client:
            for metadata in candidates:
                if (await client.get(metadata)).status_code == 200:
                    return True
    except httpx.HTTPError:
        return False
    return False


class _LoopbackCallback:
    """One-shot ``127.0.0.1`` listener for the OAuth redirect."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._future: asyncio.Future[tuple[str, str | None]] | None = None

    async def start(self, preferred_port: int | None = None) -> str:
        """Bind ``preferred_port`` (or any free port) and return the redirect URI."""
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._server = None
        if preferred_port:
            try:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", preferred_port)
            except OSError as exc:
                logger.info(
                    "mcp oauth redirect port unavailable %s",
                    kv(port=preferred_port, error=type(exc).__name__),
                )
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets or ()), None)
        if sock is None:
            raise MCPAuthError("mcp", "Could not listen for the sign-in redirect.")
        port = sock.getsockname()[1]
        return f"http://127.0.0.1:{port}{_CALLBACK_PATH}"

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
        status, page = b"200 OK", _PAGE_FAIL
        try:
            raw = await asyncio.wait_for(reader.read(8192), timeout=5)
            head = raw.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            target = head.split(" ")[1] if head.startswith("GET ") and " " in head else "/"
            parsed = urlparse(target)
            if parsed.path != _CALLBACK_PATH:
                status, page = b"404 Not Found", b""
                return
            query = parse_qs(parsed.query)
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
                b"HTTP/1.1 " + status + b"\r\n"
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


@contextlib.contextmanager
def _quiet_sdk_logs() -> Iterator[None]:
    """Keep the MCP SDK from writing a traceback to stderr during sign-in."""
    sdk_logger = logging.getLogger("mcp")
    previous = sdk_logger.level
    sdk_logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        sdk_logger.setLevel(previous)


class _StagedTokenStorage:
    """SDK token storage for one sign-in.

    Nothing reaches disk until ``commit``, so a cancelled or failed sign-in
    leaves the previous one usable.
    """

    def __init__(
        self, storage: FileTokenStorage, client_info: Any | None, *, keep_tokens: bool
    ) -> None:
        self.storage = storage
        self._client_info = client_info
        self._keep_tokens = keep_tokens
        self._tokens: Any | None = None

    @property
    def has_new_tokens(self) -> bool:
        return self._tokens is not None

    async def get_tokens(self) -> Any | None:
        if self._tokens is not None or not self._keep_tokens:
            return self._tokens
        return await self.storage.get_tokens()

    async def set_tokens(self, tokens: Any) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> Any | None:
        return self._client_info

    async def set_client_info(self, client_info: Any) -> None:
        self._client_info = client_info

    async def commit(self, *, token_endpoint: str | None, resource: str | None) -> None:
        if self._tokens is None or self._client_info is None:
            return
        await self.storage.save_login(
            self._client_info,
            self._tokens,
            token_endpoint=token_endpoint,
            resource=resource,
        )


def _preregistered_client(
    redirect_uri: str, client_id: str, client_secret: str | None = None
) -> Any:
    """Client information for an app the user already registered with this server.

    The browser flow then skips dynamic registration and uses this client.
    """
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    secret = (client_secret or "").strip() or None
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secret,
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post" if secret else "none",
        client_name="Spaces",
    )


def _redirect_uris(client_info: Any | None) -> set[str]:
    if client_info is None:
        return set()
    return {str(item) for item in client_info.redirect_uris or ()}


def _stored_redirect_port(client_info: Any | None) -> int | None:
    """Loopback port of a previous sign-in, so its registration stays valid."""
    for uri in sorted(_redirect_uris(client_info)):
        parsed = urlparse(uri)
        if parsed.hostname == "127.0.0.1" and parsed.path == _CALLBACK_PATH and parsed.port:
            return parsed.port
    return None


def _stage_login(
    storage: FileTokenStorage,
    stored_client: Any | None,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None,
) -> _StagedTokenStorage:
    if client_id:
        same_client = stored_client is not None and stored_client.client_id == client_id
        return _StagedTokenStorage(
            storage,
            _preregistered_client(redirect_uri, client_id, client_secret),
            keep_tokens=same_client,
        )
    if redirect_uri in _redirect_uris(stored_client):
        return _StagedTokenStorage(storage, stored_client, keep_tokens=True)
    # The SDK only registers when no client is stored, and the stored one
    # points the browser at a port that is not listening.
    return _StagedTokenStorage(storage, None, keep_tokens=False)


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
    redirect_port: int | None = None,
) -> dict[str, Any]:
    """Run one interactive OAuth login. Prints ``{"url": ...}`` when the browser should open.

    Returns ``{"ok": True, "signed_in": True}`` after tokens are stored, or
    ``signed_in`` false when the server accepted the connection without OAuth.
    ``open_browser`` opens the system browser from this process. The app passes
    false and opens the URL itself. ``redirect_port`` pins the loopback port for
    a client registered with an exact redirect URI; otherwise the previous
    sign-in's port is reused when free.
    """
    from mcp.client.auth import OAuthClientProvider

    cleaned = url.strip()
    if not cleaned:
        raise MCPAuthError("mcp", "This tool has no URL to sign in to.")
    storage = FileTokenStorage(storage_dir, cleaned)
    try:
        stored_client = await storage.get_client_info()
    except MCPAuthError:
        stored_client = None
    loopback = _LoopbackCallback()
    with _quiet_sdk_logs():
        try:
            redirect_uri = await loopback.start(
                redirect_port or _stored_redirect_port(stored_client)
            )
            staged = _stage_login(
                storage,
                stored_client,
                redirect_uri,
                (client_id or "").strip(),
                client_secret,
            )
            provider = OAuthClientProvider(
                server_url=cleaned,
                client_metadata=_client_metadata(redirect_uri),
                storage=staged,
                redirect_handler=lambda auth_url: _open_authorization(auth_url, open_browser),
                callback_handler=loopback.wait,
                timeout=_LOGIN_TIMEOUT_S,
            )
            response = await _post_initialize(cleaned, provider)
            return await _login_result(staged, provider, response, cleaned)
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
    staged: _StagedTokenStorage,
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
    if staged.has_new_tokens:
        meta = provider.context.oauth_metadata
        await staged.commit(
            token_endpoint=str(meta.token_endpoint) if meta is not None else None,
            resource=provider.context.get_resource_url(),
        )
    tokens = await staged.get_tokens()
    signed_in = tokens is not None and bool(tokens.access_token)
    logger.info(
        "mcp oauth sign-in finished %s",
        kv(host=_host_of(url), signed_in=signed_in),
    )
    return {"ok": True, "signed_in": signed_in}


def _host_of(url: str) -> str:
    host = urlparse(url).netloc
    return host or "unknown"
