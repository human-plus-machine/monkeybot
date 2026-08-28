"""In-app CDP preference + daemon bounce for Monkeyapp's embedded Chromium."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from browser_mcp import server


@pytest.fixture(autouse=True)
def _reset_bh_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    original_bh = server._bh
    original_bound = server._bound_cdp
    original_env_flag = server._env_set_from_in_app_file
    server._bh = None
    server._bound_cdp = None
    server._env_set_from_in_app_file = False
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    yield
    server._bh = original_bh
    server._bound_cdp = original_bound
    server._env_set_from_in_app_file = original_env_flag


def _install_fake_harness(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Stub ``browser_harness`` so ``from browser_harness import admin, helpers`` works."""
    admin = MagicMock()
    helpers = MagicMock()
    mod = ModuleType("browser_harness")
    mod.admin = admin
    mod.helpers = helpers
    monkeypatch.setitem(sys.modules, "browser_harness", mod)
    return admin, helpers


def test_apply_in_app_cdp_url_prefers_file_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale mcp.json-baked ports must not win over Monkeyapp's live bridge file."""
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    bound = server._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert server.os.environ.get("BU_CDP_WS") == bound
    assert "BU_CDP_URL" not in server.os.environ


def test_apply_in_app_cdp_url_reads_http_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333\n", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = server._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert server.os.environ.get("BU_CDP_WS") == bound
    assert "BU_CDP_URL" not in server.os.environ


def test_apply_in_app_cdp_url_reads_ws_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    assert server._apply_in_app_cdp_url() == "ws://127.0.0.1:9333/devtools"
    assert server.os.environ.get("BU_CDP_WS") == "ws://127.0.0.1:9333/devtools"
    assert "BU_CDP_URL" not in server.os.environ


def test_apply_in_app_cdp_url_attaches_token_to_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token\n", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = server._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token"
    assert server.os.environ.get("BU_CDP_WS") == bound
    assert "BU_CDP_URL" not in server.os.environ


def test_apply_in_app_cdp_url_replaces_empty_query_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot?token=", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = server._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token"


def test_apply_in_app_cdp_url_ignores_stale_token_without_url_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover token file must not rewrite operator CDP env."""
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    assert server._apply_in_app_cdp_url() == "http://127.0.0.1:9222"
    assert server.os.environ.get("BU_CDP_URL") == "http://127.0.0.1:9222"
    assert "BU_CDP_WS" not in server.os.environ


def test_apply_in_app_cdp_url_does_not_attach_token_to_cloud_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setenv("BU_CDP_WS", "wss://cloud.example/cdp")

    assert server._apply_in_app_cdp_url() == "wss://cloud.example/cdp"
    assert server.os.environ.get("BU_CDP_WS") == "wss://cloud.example/cdp"


def test_apply_in_app_cdp_url_ignores_non_loopback_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("wss://evil.example/devtools?token=stolen", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("local-token", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    assert server._apply_in_app_cdp_url() == "http://127.0.0.1:9222"
    assert "local-token" not in (server.os.environ.get("BU_CDP_WS") or "")
    assert "local-token" not in (server.os.environ.get("BU_CDP_URL") or "")


def test_raise_rewritten_in_app_cdp_error_replaces_chrome_popup_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    with pytest.raises(RuntimeError, match="Spaces browser"):
        server._raise_rewritten_in_app_cdp_error(
            RuntimeError(
                "permission-blocked: Chrome is reachable, but the per-session "
                "Allow remote debugging popup has not been accepted"
            )
        )


def test_raise_rewritten_in_app_cdp_error_matches_ws_handshake_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    with pytest.raises(RuntimeError, match="Spaces browser"):
        server._raise_rewritten_in_app_cdp_error(
            RuntimeError("ws handshake failed: HTTP 403 Forbidden")
        )


def test_raise_rewritten_in_app_cdp_error_ignores_stale_token_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")

    server._raise_rewritten_in_app_cdp_error(
        RuntimeError("ws handshake failed: HTTP 403 Forbidden")
    )


def test_apply_in_app_cdp_url_missing_file_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", tmp_path / "missing")
    assert server._apply_in_app_cdp_url() is None


def test_apply_in_app_cdp_url_clears_env_once_in_app_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeyapp closes -> file goes away -> must not fall back to the dead port

    it wrote into env on a previous call (that's the exact stale-endpoint bug
    this module exists to avoid)."""
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = server._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert server.os.environ.get("BU_CDP_WS") == bound

    cdp_file.unlink()

    assert server._apply_in_app_cdp_url() is None
    assert "BU_CDP_URL" not in server.os.environ
    assert "BU_CDP_WS" not in server.os.environ


def test_apply_in_app_cdp_url_preserves_operator_env_once_in_app_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-supplied BU_CDP_URL (self-hosted headless / Browser Use Cloud,
    see docs/browser-mcp.md) must never be cleared just because it's later
    shadowed by an in-app file that then disappears."""
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    assert server._apply_in_app_cdp_url() == "ws://127.0.0.1:9333/devtools/browser/monkeybot"

    cdp_file.unlink()

    # Operator env was overwritten by the in-app value while the file existed,
    # so once the file is gone there is no way to recover the original
    # "http://127.0.0.1:9222" -- only that env vars aren't left pointing at
    # the dead in-app port.
    assert server._apply_in_app_cdp_url() is None
    assert "BU_CDP_URL" not in server.os.environ


def test_apply_in_app_cdp_url_logs_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "blocked-dir"
    blocked.mkdir()
    # Point at a directory so read_text raises IsADirectoryError (OSError subclass).
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", blocked)

    with caplog.at_level(logging.WARNING, logger="browser_mcp.server"):
        assert server._apply_in_app_cdp_url() is None

    assert any("failed reading in-app CDP URL file" in r.message for r in caplog.records)


def test_browser_harness_bounces_alive_daemon_when_cdp_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CDP set + external daemon alive (local Chrome) must restart before ensure.

    Regression: previously called nonexistent admin.daemon_browser_kind().
    """
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True

    result = server._browser_harness()

    assert result == (helpers, admin)
    admin.restart_daemon.assert_called_once()
    admin.ensure_daemon.assert_called_once()
    assert server._bound_cdp == "http://127.0.0.1:9333"
    assert "daemon_browser_kind" not in [c[0] for c in admin.method_calls]


def test_browser_harness_does_not_bounce_when_already_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    helpers = MagicMock()
    admin = MagicMock()
    server._bh = (helpers, admin)
    server._bound_cdp = "http://127.0.0.1:9333"

    result = server._browser_harness()

    assert result == (helpers, admin)
    admin.daemon_alive.assert_not_called()
    admin.restart_daemon.assert_not_called()
    admin.ensure_daemon.assert_not_called()


def test_browser_harness_no_bounce_without_cdp_when_daemon_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True

    result = server._browser_harness()

    assert result == (helpers, admin)
    admin.restart_daemon.assert_not_called()
    admin.ensure_daemon.assert_called_once()
    assert server._bound_cdp is None


def test_browser_harness_rebounds_when_cdp_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True
    server._bh = (helpers, admin)
    server._bound_cdp = "http://127.0.0.1:9222"
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")

    result = server._browser_harness()

    assert result == (helpers, admin)
    admin.restart_daemon.assert_called_once()
    admin.ensure_daemon.assert_called_once()
    assert server._bound_cdp == "http://127.0.0.1:9333"


class _FakeHttpResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _publish_loopback_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)


def test_sealed_login_posts_bearer_without_query_token_or_host_spoof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    captured: dict[str, Request] = {}

    def fake_urlopen(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "password": "leaked"})

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    result = server._sealed_login("alice")

    req = captured["req"]
    assert result == {"ok": True, "loggedIn": True}
    assert req.full_url == "http://127.0.0.1:9333/json/login"
    assert "token=" not in req.full_url
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert req.get_method() == "POST"


def test_sealed_login_refuses_non_loopback_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("wss://evil.example/devtools?token=stolen", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("local-token", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    def fail_urlopen(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not POST login off loopback")

    monkeypatch.setattr(server.urllib.request, "urlopen", fail_urlopen)

    assert server._sealed_login(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "in-app browser is not available",
    }


def test_sealed_login_maps_unknown_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_urlopen(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 500, "boom", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    assert server._sealed_login(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "login failed",
    }


def test_sealed_login_keeps_allowlisted_bridge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_urlopen(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(
            {"ok": False, "loggedIn": False, "error": "this password is not allowed for agent use"}
        )

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    assert server._sealed_login(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "this password is not allowed for agent use",
    }


def test_in_app_http_origin_keeps_https_for_wss() -> None:
    assert (
        server._in_app_http_origin("wss://127.0.0.1:9333/devtools/browser/monkeybot")
        == "https://127.0.0.1:9333"
    )
    assert server._in_app_http_origin("wss://cloud.example/cdp") is None
