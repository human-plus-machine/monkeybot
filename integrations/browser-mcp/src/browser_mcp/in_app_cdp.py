"""In-app Electron CDP URL/token resolution and public error rewriting."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

# Written by Monkeyapp when the in-app Electron CDP bridge is live. Prefer this over
# auto-discovering the user's desktop Chrome (which wins when BU_CDP_URL is empty/stale).
_IN_APP_CDP_URL_FILE = Path.home() / ".monkeybot" / "runtime" / "in-app-cdp-url"

# True when the last _apply_in_app_cdp_url() call set BU_CDP_URL/WS from the in-app file
# (as opposed to an operator-supplied env var). Lets us clear that self-set value when the
# file goes away, instead of falling back to a port we wrote from a now-stale file read.
_env_set_from_in_app_file = False

# Matches monkeyapp BROWSER_TARGET_ID ('monkeybot'). The Spaces upgrade handler
# accepts any /devtools/browser/* path, so this only has to stay in sync for the
# URL we advertise, not for routing.
_IN_APP_BROWSER_WS_PATH = "/devtools/browser/monkeybot"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_IN_APP_CDP_REJECTED = (
    "in-app browser CDP rejected the connection. This is the Spaces browser, "
    "not Google Chrome — there is no Allow-remote-debugging popup to click. "
    "Open the Browser panel in Spaces and retry."
)
_TOKEN_QUERY_RE = re.compile(r"([?&]token=)[^&\s]*", re.IGNORECASE)


def _read_in_app_cdp_file() -> str | None:
    try:
        raw = _IN_APP_CDP_URL_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "browser-mcp: failed reading in-app CDP URL file %s",
            _IN_APP_CDP_URL_FILE,
            exc_info=True,
        )
        return None
    return raw or None


def _read_in_app_cdp_token() -> str | None:
    token_file = _IN_APP_CDP_URL_FILE.parent / "in-app-cdp-token"
    try:
        raw = token_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "browser-mcp: failed reading in-app CDP token file %s",
            token_file,
            exc_info=True,
        )
        return None
    return raw or None


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    return host.lower().strip("[]") in _LOOPBACK_HOSTS


def _query_token(query: str) -> str | None:
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == "token":
            return value or None
    return None


def _with_query_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "token"
    ]
    pairs.append(("token", token))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _in_app_ws_url(url: str, token: str | None) -> str:
    """browser-harness treats HTTP 403 as Chrome's Allow-debugging popup.

    The in-app bridge requires a bearer/query token on every request, including
    ``/json/version``. Point the daemon at the tokenized WebSocket URL so it
    never does that unauthenticated HTTP probe. The local token is only ever
    attached to a loopback URL. A query token already on the published URL
    wins over a leftover token file — Spaces writes the URL first.
    """
    parsed = urlparse(url)
    if not _is_loopback_host(parsed.hostname):
        return url
    attached = _query_token(parsed.query) or token
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        url = f"{scheme}://{host}:{port}{_IN_APP_BROWSER_WS_PATH}"
    return _with_query_token(url, attached)


def _redact_cdp_token(message: str) -> str:
    """Strip query tokens so harness log tails cannot leak into agent transcripts."""
    return _TOKEN_QUERY_RE.sub(r"\1[redacted]", message)


def _bind_in_app_endpoint(url: str, token: str | None) -> str:
    global _env_set_from_in_app_file
    endpoint = _in_app_ws_url(url, token)
    os.environ["BU_CDP_WS"] = endpoint
    os.environ.pop("BU_CDP_URL", None)
    _env_set_from_in_app_file = True
    return endpoint


def _looks_like_in_app_cdp_rejection(message: str) -> bool:
    lower = message.lower()
    if "permission-blocked" in lower or "allow remote debugging" in lower:
        return True
    return "ws handshake failed" in lower and "403" in lower


def _raise_rewritten_in_app_cdp_error(exc: BaseException) -> None:
    """Replace Chrome-popup copy when a 403 actually came from Spaces.

    Only rewrite when the in-app URL file is published (or we just bound it).
    A leftover token file is not evidence the bridge is live.
    """
    if not _looks_like_in_app_cdp_rejection(str(exc)):
        return
    if not (_read_in_app_cdp_file() or _env_set_from_in_app_file):
        return
    # Drop the cause chain so a tokenized WS URL in the original message
    # cannot leak into the agent transcript.
    raise RuntimeError(_IN_APP_CDP_REJECTED) from None


def _reraise_public_harness_error(exc: BaseException) -> NoReturn:
    """Rewrite in-app 403s and strip query tokens before they reach the agent.

    Call only from an ``except`` block. Unchanged messages are re-raised as-is.
    """
    _raise_rewritten_in_app_cdp_error(exc)
    message = _redact_cdp_token(str(exc))
    if message != str(exc):
        raise RuntimeError(message) from None
    raise


def _apply_in_app_cdp_url() -> str | None:
    """Ensure BU_CDP_URL/WS points at Monkeyapp's bridge when one is published.

    Prefers the in-app runtime file over process env: mcp.json often bakes a
    concrete ``http://127.0.0.1:PORT`` from a previous launch, and that port is
    dead after restart (WinError 10061 / connection refused). The file is
    rewritten every time Electron's CDP bridge comes up.

    When the file disappears (Monkeyapp closed) after having supplied the
    active endpoint, the env var we set from it is cleared rather than left in
    place -- otherwise the next call would fall back to that same self-set,
    now-dead port, reintroducing the stale-endpoint bug this function exists
    to avoid. Operator-supplied env vars (self-hosted headless Chrome, Browser
    Use Cloud -- see docs/browser-mcp.md) are never touched by this path.

    Returns the explicit CDP endpoint in use (file or env), or None.
    """
    global _env_set_from_in_app_file

    file_url = _read_in_app_cdp_file()
    token = _read_in_app_cdp_token()
    if file_url and _is_loopback_host(urlparse(file_url).hostname):
        return _bind_in_app_endpoint(file_url, token)

    if _env_set_from_in_app_file:
        os.environ.pop("BU_CDP_WS", None)
        os.environ.pop("BU_CDP_URL", None)
        _env_set_from_in_app_file = False

    ws = (os.environ.get("BU_CDP_WS") or "").strip()
    http = (os.environ.get("BU_CDP_URL") or "").strip()
    if ws:
        return ws
    if http:
        return http
    return None
