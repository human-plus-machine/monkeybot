"""Sealed in-app login over the local CDP HTTP bridge."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from browser_mcp import backend, in_app_cdp

logger = logging.getLogger(__name__)

_LOGIN_PUBLIC_ERRORS = frozenset(
    {
        "in-app browser is not available",
        "in-app browser token is missing",
        "in-app browser could not verify the origin",
        "no tab",
        "not a web page",
        "focused tab is on a different origin",
        "no saved password for this site",
        "this password is not allowed for agent use",
        "login needs your attention",
        "waiting for your approval",
        "agent access denied for this site",
        "grant expired",
        "mfa needs your attention",
        "unexpected login response",
        "login failed",
    }
)
_LOGIN_PUBLIC_MFA_VALUES = frozenset({"none", "completed", "needed"})
_LOGIN_PUBLIC_MODE_VALUES = frozenset({"keystroke", "network"})
# Passing ProxyHandler({}) makes urllib skip its default env-based proxy
# handler. The empty handler itself is then dropped (no *_open methods),
# so loopback requests never honor HTTP_PROXY.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# 150s covers the bridge's 120s "ask" approval window plus normal login time.
_LOGIN_TIMEOUT_S = 150


def _format_loopback_host(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _in_app_http_origin(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        return None
    if not in_app_cdp._is_loopback_host(parsed.hostname) or not parsed.hostname:
        return None
    http_scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
    default_port = 443 if http_scheme == "https" else 80
    port = parsed.port or default_port
    return f"{http_scheme}://{_format_loopback_host(parsed.hostname)}:{port}"


def _in_app_http_and_token() -> tuple[str | None, str | None]:
    """HTTP origin + bearer token for the published in-app CDP bridge only."""
    in_app_cdp._apply_in_app_cdp_url()
    file_url = in_app_cdp._read_in_app_cdp_file()
    if not file_url:
        return None, None
    http = _in_app_http_origin(file_url)
    if not http:
        return None, None
    token = in_app_cdp._query_token(urlparse(file_url).query) or in_app_cdp._read_in_app_cdp_token()
    return http, token


def _public_login_result(body: dict[str, Any]) -> dict[str, Any]:
    """Copy only the non-secret fields out of the bridge response.

    Built by allowlist rather than by stripping keys, so a future bridge field
    cannot introduce a credential leak here.
    """
    error = body.get("error")
    origin = body.get("origin")
    result: dict[str, Any] = {
        "ok": bool(body.get("ok")),
        "loggedIn": bool(body.get("loggedIn")),
    }
    # The origin the bridge actually acted on. The agent cannot otherwise tell:
    # the bridge logs in to the tab the *user* has focused, while harness tool
    # calls address tabs by CDP session, so browser_switch_tab (or the user
    # clicking another tab) makes the two diverge.
    if isinstance(origin, str) and origin:
        result["origin"] = origin
    mfa = body.get("mfa")
    if isinstance(mfa, str) and mfa in _LOGIN_PUBLIC_MFA_VALUES:
        result["mfa"] = mfa
    mode = body.get("mode")
    if isinstance(mode, str) and mode in _LOGIN_PUBLIC_MODE_VALUES:
        result["mode"] = mode
    if error:
        message = str(error)
        result["error"] = message if message in _LOGIN_PUBLIC_ERRORS else "login failed"
    return result


def _loopback_open(req: urllib.request.Request, timeout: float = _LOGIN_TIMEOUT_S) -> Any:
    return _LOOPBACK_OPENER.open(req, timeout=timeout)


def _sealed_login(username: str | None, expected_origin: str | None) -> dict[str, Any]:
    if backend._bound_cdp == "agentcore":
        return {"ok": False, "loggedIn": False, "error": "in-app browser is not available"}
    http, token = _in_app_http_and_token()
    if not http:
        return {"ok": False, "loggedIn": False, "error": "in-app browser is not available"}
    if not token:
        return {"ok": False, "loggedIn": False, "error": "in-app browser token is missing"}
    request_body: dict[str, str] = {}
    if username:
        request_body["username"] = username
    if expected_origin:
        request_body["expectedOrigin"] = expected_origin
    payload = json.dumps(request_body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        **in_app_cdp._run_headers(),
    }
    req = urllib.request.Request(f"{http}/json/login", data=payload, headers=headers, method="POST")
    try:
        with _loopback_open(req, timeout=_LOGIN_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) in {401, 403}:
            return {"ok": False, "loggedIn": False, "error": "in-app browser token is missing"}
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            logger.warning("browser_login HTTP %s", getattr(exc, "code", "?"))
            return {"ok": False, "loggedIn": False, "error": "login failed"}
    except Exception:
        logger.warning("browser_login failed", exc_info=True)
        return {"ok": False, "loggedIn": False, "error": "login failed"}
    if not isinstance(body, dict):
        return {"ok": False, "loggedIn": False, "error": "unexpected login response"}
    result = _public_login_result(body)
    if expected_origin and "origin" not in result:
        # A Spaces build older than expectedOrigin support ignores it and echoes
        # no origin, so the login it just performed is unverifiable. Report that
        # rather than letting the agent treat an unchecked login as confirmed.
        result = {
            "ok": False,
            "loggedIn": result["loggedIn"],
            "error": "in-app browser could not verify the origin",
        }
    return result
