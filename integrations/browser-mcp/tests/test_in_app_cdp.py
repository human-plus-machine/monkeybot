"""In-app CDP preference + daemon bounce for Monkeyapp's embedded Chromium."""

from __future__ import annotations

import os

import inspect
import json
import logging
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request

import pytest
from browser_mcp import server, backend, in_app_cdp, login


@pytest.fixture(autouse=True)
def _reset_bh_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    original_bh = backend._bh
    original_bound = backend._bound_cdp
    original_env_flag = in_app_cdp._env_set_from_in_app_file
    backend._bh = None
    backend._bound_cdp = None
    in_app_cdp._env_set_from_in_app_file = False
    monkeypatch.delenv("BU_CDP_URL", raising=False)
    monkeypatch.delenv("BU_CDP_WS", raising=False)
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    yield
    backend._bh = original_bh
    backend._bound_cdp = original_bound
    in_app_cdp._env_set_from_in_app_file = original_env_flag


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
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert os.environ.get("BU_CDP_WS") == bound
    assert "BU_CDP_URL" not in os.environ


def test_apply_in_app_cdp_url_reads_http_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333\n", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert os.environ.get("BU_CDP_WS") == bound
    assert "BU_CDP_URL" not in os.environ


def test_apply_in_app_cdp_url_reads_ws_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    assert in_app_cdp._apply_in_app_cdp_url() == "ws://127.0.0.1:9333/devtools"
    assert os.environ.get("BU_CDP_WS") == "ws://127.0.0.1:9333/devtools"
    assert "BU_CDP_URL" not in os.environ


def test_apply_in_app_cdp_url_attaches_token_to_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token\n", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token"
    assert os.environ.get("BU_CDP_WS") == bound
    assert "BU_CDP_URL" not in os.environ


def test_apply_in_app_cdp_url_replaces_empty_query_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot?token=", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token"


def test_apply_in_app_cdp_url_query_token_wins_over_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spaces writes the URL file (with the fresh query token) before the token file."""
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text(
        "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=from-url",
        encoding="utf-8",
    )
    (tmp_path / "in-app-cdp-token").write_text("from-file", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=from-url"


def test_apply_in_app_cdp_url_http_file_keeps_query_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333?token=from-url\n", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=from-url"


def test_apply_in_app_cdp_url_attaches_run_param_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-123")

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token&run=run-123"


def test_apply_in_app_cdp_url_omits_run_param_without_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token"
    assert "run=" not in bound


def test_run_headers_empty_without_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    assert in_app_cdp._run_headers() == {}


def test_run_headers_include_label_when_run_id_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-123")
    monkeypatch.setenv("MONKEYBOT_RUN_LABEL", "Routine: Nightly export")
    assert in_app_cdp._run_headers() == {
        "X-Monkeybot-Run": "run-123",
        "X-Monkeybot-Run-Label": "Routine: Nightly export",
    }


def test_run_headers_default_label_is_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "session-key")
    monkeypatch.delenv("MONKEYBOT_RUN_LABEL", raising=False)
    assert in_app_cdp._run_headers() == {
        "X-Monkeybot-Run": "session-key",
        "X-Monkeybot-Run-Label": "Chat",
    }


def test_apply_in_app_cdp_url_ignores_stale_token_without_url_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover token file must not rewrite operator CDP env."""
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    assert in_app_cdp._apply_in_app_cdp_url() == "http://127.0.0.1:9222"
    assert os.environ.get("BU_CDP_URL") == "http://127.0.0.1:9222"
    assert "BU_CDP_WS" not in os.environ


def test_apply_in_app_cdp_url_does_not_attach_token_to_cloud_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")
    monkeypatch.setenv("BU_CDP_WS", "wss://cloud.example/cdp")

    assert in_app_cdp._apply_in_app_cdp_url() == "wss://cloud.example/cdp"
    assert os.environ.get("BU_CDP_WS") == "wss://cloud.example/cdp"


def test_apply_in_app_cdp_url_ignores_non_loopback_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("wss://evil.example/devtools?token=stolen", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("local-token", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")

    assert in_app_cdp._apply_in_app_cdp_url() == "http://127.0.0.1:9222"
    assert "local-token" not in (os.environ.get("BU_CDP_WS") or "")
    assert "local-token" not in (os.environ.get("BU_CDP_URL") or "")


def test_raise_rewritten_in_app_cdp_error_replaces_chrome_popup_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    with pytest.raises(RuntimeError, match="Spaces browser") as excinfo:
        in_app_cdp._raise_rewritten_in_app_cdp_error(
            RuntimeError(
                "permission-blocked: Chrome is reachable, but the per-session "
                "Allow remote debugging popup has not been accepted"
            )
        )
    assert excinfo.value.__cause__ is None


def test_raise_rewritten_in_app_cdp_error_matches_ws_handshake_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    with pytest.raises(RuntimeError, match="Spaces browser"):
        in_app_cdp._raise_rewritten_in_app_cdp_error(
            RuntimeError("ws handshake failed: HTTP 403 Forbidden")
        )


def test_raise_rewritten_in_app_cdp_error_ignores_stale_token_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", tmp_path / "in-app-cdp-url")
    (tmp_path / "in-app-cdp-token").write_text("secret-token", encoding="utf-8")

    in_app_cdp._raise_rewritten_in_app_cdp_error(
        RuntimeError("ws handshake failed: HTTP 403 Forbidden")
    )


def test_apply_in_app_cdp_url_missing_file_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", tmp_path / "missing")
    assert in_app_cdp._apply_in_app_cdp_url() is None


def test_apply_in_app_cdp_url_clears_env_once_in_app_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeyapp closes -> file goes away -> must not fall back to the dead port

    it wrote into env on a previous call (that's the exact stale-endpoint bug
    this module exists to avoid)."""
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    bound = in_app_cdp._apply_in_app_cdp_url()
    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert os.environ.get("BU_CDP_WS") == bound

    cdp_file.unlink()

    assert in_app_cdp._apply_in_app_cdp_url() is None
    assert "BU_CDP_URL" not in os.environ
    assert "BU_CDP_WS" not in os.environ


def test_apply_in_app_cdp_url_preserves_operator_env_once_in_app_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-supplied BU_CDP_URL (self-hosted headless / Browser Use Cloud,
    see docs/browser-mcp.md) must never be cleared just because it's later
    shadowed by an in-app file that then disappears."""
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9222")
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    assert in_app_cdp._apply_in_app_cdp_url() == "ws://127.0.0.1:9333/devtools/browser/monkeybot"

    cdp_file.unlink()

    # Operator env was overwritten by the in-app value while the file existed,
    # so once the file is gone there is no way to recover the original
    # "http://127.0.0.1:9222" -- only that env vars aren't left pointing at
    # the dead in-app port.
    assert in_app_cdp._apply_in_app_cdp_url() is None
    assert "BU_CDP_URL" not in os.environ


def test_apply_in_app_cdp_url_logs_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "blocked-dir"
    blocked.mkdir()
    # Point at a directory so read_text raises IsADirectoryError (OSError subclass).
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", blocked)

    with caplog.at_level(logging.WARNING, logger="browser_mcp.in_app_cdp"):
        assert in_app_cdp._apply_in_app_cdp_url() is None

    assert any("failed reading in-app CDP URL file" in r.message for r in caplog.records)


def test_apply_in_app_cdp_url_logs_token_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").mkdir()
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    with caplog.at_level(logging.WARNING, logger="browser_mcp.in_app_cdp"):
        bound = in_app_cdp._apply_in_app_cdp_url()

    assert bound == "ws://127.0.0.1:9333/devtools/browser/monkeybot"
    assert any("failed reading in-app CDP token file" in r.message for r in caplog.records)


def test_browser_harness_bounces_alive_daemon_when_cdp_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CDP set + external daemon alive (local Chrome) must restart before ensure.

    Regression: previously called nonexistent admin.daemon_browser_kind().
    """
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True

    result = backend.browser_harness()

    assert result == (helpers, admin)
    admin.restart_daemon.assert_called_once()
    admin.ensure_daemon.assert_called_once()
    assert backend._bound_cdp == "http://127.0.0.1:9333"
    assert "daemon_browser_kind" not in [c[0] for c in admin.method_calls]


def test_browser_harness_does_not_bounce_when_already_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")
    helpers = MagicMock()
    admin = MagicMock()
    backend._bh = (helpers, admin)
    backend._bound_cdp = "http://127.0.0.1:9333"

    result = backend.browser_harness()

    assert result == (helpers, admin)
    admin.daemon_alive.assert_not_called()
    admin.restart_daemon.assert_not_called()
    admin.ensure_daemon.assert_not_called()


def test_browser_harness_no_bounce_without_cdp_when_daemon_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True

    result = backend.browser_harness()

    assert result == (helpers, admin)
    admin.restart_daemon.assert_not_called()
    admin.ensure_daemon.assert_called_once()
    assert backend._bound_cdp is None


def test_browser_harness_rebounds_when_cdp_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True
    backend._bh = (helpers, admin)
    backend._bound_cdp = "http://127.0.0.1:9222"
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9333")

    result = backend.browser_harness()

    assert result == (helpers, admin)
    admin.restart_daemon.assert_called_once()
    admin.ensure_daemon.assert_called_once()
    assert backend._bound_cdp == "http://127.0.0.1:9333"


def test_redact_cdp_token_strips_query_value() -> None:
    raw = "connecting to ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret-token"
    redacted = in_app_cdp._redact_cdp_token(raw)
    assert "secret-token" not in redacted
    assert "token=[redacted]" in redacted


def test_tool_redacts_token_in_ensure_daemon_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret")
    admin, _helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = False
    admin.ensure_daemon.side_effect = RuntimeError(
        "CDP WS handshake failed: connecting to "
        "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret -- remote down"
    )

    with pytest.raises(RuntimeError) as excinfo:
        server.browser_page_info()

    assert "secret" not in str(excinfo.value)
    assert "token=[redacted]" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_tool_redacts_token_in_restart_daemon_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret")
    admin, _helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = True
    backend._bound_cdp = "http://127.0.0.1:9222"
    admin.restart_daemon.side_effect = RuntimeError(
        "connecting to ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret"
    )

    with pytest.raises(RuntimeError) as excinfo:
        server.browser_page_info()

    assert "secret" not in str(excinfo.value)
    assert "token=[redacted]" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_tool_redacts_token_raised_after_daemon_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon can hand back a tokenized endpoint long after ensure_daemon()."""
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret")
    admin, helpers = _install_fake_harness(monkeypatch)
    admin.daemon_alive.return_value = False
    helpers.page_info.side_effect = RuntimeError(
        "cdp disconnected: ws://127.0.0.1:9333/devtools/browser/monkeybot?token=secret"
    )

    with pytest.raises(RuntimeError) as excinfo:
        server.browser_page_info()

    assert "secret" not in str(excinfo.value)
    assert "token=[redacted]" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_every_registered_tool_is_wrapped_for_redaction() -> None:
    """New tools must not be able to opt out of redaction by forgetting a decorator."""
    unwrapped = [
        name
        for name, obj in vars(server).items()
        if name.startswith("browser_")
        and callable(obj)
        and getattr(obj, "__wrapped__", None) is None
    ]
    assert unwrapped == []


def test_public_tool_preserves_tool_schema() -> None:
    """FastMCP builds the arg schema from the signature, so wrapping must be transparent."""
    sig = inspect.signature(server.browser_login)
    assert list(sig.parameters) == ["username", "expected_origin"]
    assert server.browser_login.__name__ == "browser_login"
    assert server.browser_login.__doc__ is not None


def test_loopback_opener_ignores_http_proxy() -> None:
    """Empty ProxyHandler({}) suppresses urllib's default env-based proxy handler."""
    assert not any(isinstance(h, ProxyHandler) for h in login._LOOPBACK_OPENER.handlers)


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
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)


def test_sealed_login_posts_bearer_without_query_token_or_host_spoof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "password": "leaked"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    result = login._sealed_login("alice", None)

    req = captured["req"]
    assert isinstance(req, Request)
    assert result == {"ok": True, "loggedIn": True}
    assert req.full_url == "http://127.0.0.1:9333/json/login"
    assert "token=" not in req.full_url
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert req.get_method() == "POST"
    assert req.data is not None
    assert json.loads(req.data.decode("utf-8")) == {"username": "alice"}
    assert captured["timeout"] == login._LOGIN_TIMEOUT_S


def test_browser_login_tool_returns_json_without_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "password": "leaked"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert json.loads(server.browser_login("alice")) == {"ok": True, "loggedIn": True}


def test_sealed_login_returns_origin_the_bridge_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent drives tabs by CDP session, so it needs the origin to verify."""
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(
            {"ok": True, "loggedIn": True, "origin": "https://example.com", "password": "leaked"}
        )

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": True,
        "loggedIn": True,
        "origin": "https://example.com",
    }


def test_sealed_login_forwards_expected_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "origin": "https://example.com"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    login._sealed_login("alice", "https://example.com")

    req = captured["req"]
    assert isinstance(req, Request)
    assert req.data is not None
    assert json.loads(req.data.decode("utf-8")) == {
        "username": "alice",
        "expectedOrigin": "https://example.com",
    }


def test_sealed_login_sends_run_headers_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("MONKEYBOT_RUN_ID", "run-123")
    monkeypatch.setenv("MONKEYBOT_RUN_LABEL", "Routine: Nightly export")
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse({"ok": True, "loggedIn": True})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    login._sealed_login(None, None)

    req = captured["req"]
    assert isinstance(req, Request)
    assert req.get_header("X-Monkeybot-Run".capitalize()) == "run-123"
    assert req.get_header("X-Monkeybot-Run-Label".capitalize()) == "Routine: Nightly export"


def test_sealed_login_omits_run_headers_without_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    monkeypatch.delenv("MONKEYBOT_RUN_ID", raising=False)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse({"ok": True, "loggedIn": True})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    login._sealed_login(None, None)

    req = captured["req"]
    assert isinstance(req, Request)
    assert req.get_header("X-Monkeybot-Run".capitalize()) is None


def test_sealed_login_surfaces_origin_mismatch_with_actual_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatch must stay verbatim, not collapse to the generic 'login failed'."""
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {
            "ok": False,
            "loggedIn": False,
            "origin": "https://other.example",
            "error": "focused tab is on a different origin",
        }
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, "https://example.com") == {
        "ok": False,
        "loggedIn": False,
        "origin": "https://other.example",
        "error": "focused tab is on a different origin",
    }


def test_sealed_login_fails_when_bridge_cannot_verify_expected_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older Spaces build drops expectedOrigin — never report that as verified."""
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, "https://example.com") == {
        "ok": False,
        "loggedIn": True,
        "error": "in-app browser could not verify the origin",
    }


def test_sealed_login_ignores_non_string_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "origin": {"nested": "junk"}})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": True, "loggedIn": True}


def test_sealed_login_refuses_non_loopback_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("wss://evil.example/devtools?token=stolen", encoding="utf-8")
    (tmp_path / "in-app-cdp-token").write_text("local-token", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    def fail_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not POST login off loopback")

    monkeypatch.setattr(login, "_loopback_open", fail_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "in-app browser is not available",
    }


def test_sealed_login_missing_token_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools/browser/monkeybot", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)

    def fail_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not POST login without a token")

    monkeypatch.setattr(login, "_loopback_open", fail_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "in-app browser token is missing",
    }


def test_sealed_login_maps_unknown_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 500, "boom", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "login failed",
    }


def test_sealed_login_reads_allowlisted_error_from_http_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spaces returns HTTP 400 with a JSON body for every failed login."""
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {
            "ok": False,
            "loggedIn": False,
            "error": "this password is not allowed for agent use",
        }
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "this password is not allowed for agent use",
    }


def test_sealed_login_maps_http_403_to_missing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(
            req.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=BytesIO(b'{"error": "forbidden"}'),
        )

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "in-app browser token is missing",
    }


def test_sealed_login_refuses_when_agentcore_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    backend._bound_cdp = "agentcore"

    def fail_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not POST login while AgentCore is bound")

    monkeypatch.setattr(login, "_loopback_open", fail_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "in-app browser is not available",
    }


def test_sealed_login_prefers_query_token_over_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text(
        "ws://127.0.0.1:9333/devtools/browser/monkeybot?token=from-url",
        encoding="utf-8",
    )
    (tmp_path / "in-app-cdp-token").write_text("from-file", encoding="utf-8")
    monkeypatch.setattr(in_app_cdp, "_IN_APP_CDP_URL_FILE", cdp_file)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse({"ok": True, "loggedIn": True})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": True, "loggedIn": True}
    req = captured["req"]
    assert isinstance(req, Request)
    assert req.get_header("Authorization") == "Bearer from-url"


def test_sealed_login_keeps_allowlisted_bridge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(
            {"ok": False, "loggedIn": False, "error": "this password is not allowed for agent use"}
        )

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "this password is not allowed for agent use",
    }


def test_sealed_login_passes_through_needs_attention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed-closed scrub must reach the agent verbatim, not as 'login failed'."""
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {
            "ok": False,
            "loggedIn": False,
            "origin": "https://example.com",
            "error": "login needs your attention",
        }
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "origin": "https://example.com",
        "error": "login needs your attention",
    }


@pytest.mark.parametrize(
    "error",
    ["waiting for your approval", "agent access denied for this site", "grant expired"],
)
def test_sealed_login_passes_through_grant_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """The grant-flow error strings must reach the agent verbatim."""
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps({"ok": False, "loggedIn": False, "error": error}).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": False, "loggedIn": False, "error": error}


def test_sealed_login_still_maps_unknown_error_to_login_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {"ok": False, "loggedIn": False, "error": "some brand new bridge error"}
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": False,
        "error": "login failed",
    }


@pytest.mark.parametrize("mfa", ["none", "completed", "needed"])
def test_sealed_login_passes_through_known_mfa_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mfa: str
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps({"ok": True, "loggedIn": True, "mfa": mfa}).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(json.loads(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": True, "loggedIn": True, "mfa": mfa}


def test_sealed_login_drops_unknown_mfa_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowlist, not a passthrough: an unrecognized mfa value must not reach the agent."""
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "mfa": "not-a-real-value"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": True, "loggedIn": True}


def test_sealed_login_needs_mfa_error_is_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {"ok": False, "loggedIn": True, "mfa": "needed", "error": "mfa needs your attention"}
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {
        "ok": False,
        "loggedIn": True,
        "mfa": "needed",
        "error": "mfa needs your attention",
    }


@pytest.mark.parametrize("mode", ["keystroke", "network"])
def test_sealed_login_passes_through_known_mode_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps({"ok": True, "loggedIn": True, "mode": mode}).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(json.loads(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": True, "loggedIn": True, "mode": mode}


def test_sealed_login_drops_unknown_mode_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "mode": "not-a-real-mode"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_login(None, None) == {"ok": True, "loggedIn": True}


def test_in_app_http_origin_keeps_https_for_wss() -> None:
    assert (
        login._in_app_http_origin("wss://127.0.0.1:9333/devtools/browser/monkeybot")
        == "https://127.0.0.1:9333"
    )


# --- Phase 4.4 (passkeys) — UNVERIFIED against a live browser -------------
# These only cover the bridge-request/response plumbing on the monkeybot
# side (mirroring the _sealed_login tests above), not the WebAuthn CDP
# mechanics in Spaces itself. See docs/credential-broker.md there.


def test_sealed_passkey_posts_bearer_and_strips_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse(
            {"ok": True, "loggedIn": True, "origin": "https://example.com", "mode": "passkey"}
        )

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    result = login._sealed_passkey(None)

    req = captured["req"]
    assert isinstance(req, Request)
    assert result == {"ok": True, "loggedIn": True, "origin": "https://example.com", "mode": "passkey"}
    assert req.full_url == "http://127.0.0.1:9333/json/passkey"
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert req.get_method() == "POST"


def test_browser_passkey_tool_returns_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "mode": "passkey"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert json.loads(server.browser_passkey()) == {"ok": True, "loggedIn": True, "mode": "passkey"}


def test_sealed_passkey_forwards_expected_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        captured["req"] = req
        return _FakeHttpResponse({"ok": True, "loggedIn": True, "origin": "https://example.com"})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    login._sealed_passkey("https://example.com")

    req = captured["req"]
    assert isinstance(req, Request)
    assert req.data is not None
    assert json.loads(req.data.decode("utf-8")) == {"expectedOrigin": "https://example.com"}


def test_sealed_passkey_fails_when_bridge_cannot_verify_expected_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        return _FakeHttpResponse({"ok": True, "loggedIn": True})

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_passkey("https://example.com") == {
        "ok": False,
        "loggedIn": True,
        "error": "in-app browser could not verify the origin",
    }


def test_sealed_passkey_maps_unknown_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 500, "boom", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_passkey(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "login failed",
    }


def test_sealed_passkey_reads_allowlisted_error_from_http_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {"ok": False, "loggedIn": False, "error": "no saved passkey for this site"}
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_passkey(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "no saved passkey for this site",
    }


def test_sealed_passkey_passes_through_grant_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passkey shares the login grant check; a stale grant must read as 'grant
    expired', not collapse to 'login failed' and prompt a pointless retry."""
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps({"ok": False, "loggedIn": False, "error": "grant expired"}).encode(
        "utf-8"
    )

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_passkey(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "grant expired",
    }


def test_sealed_passkey_drops_unallowlisted_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error string not in the passkey allowlist (e.g. a password-only one) is not leaked."""
    _publish_loopback_bridge(tmp_path, monkeypatch)
    payload = json.dumps(
        {"ok": False, "loggedIn": False, "error": "this password is not allowed for agent use"}
    ).encode("utf-8")

    def fake_open(req: Request, timeout: object = None) -> _FakeHttpResponse:
        raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=BytesIO(payload))

    monkeypatch.setattr(login, "_loopback_open", fake_open)

    assert login._sealed_passkey(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "login failed",
    }


def test_sealed_passkey_refuses_when_agentcore_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_loopback_bridge(tmp_path, monkeypatch)
    backend._bound_cdp = "agentcore"

    def fail_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not POST passkey while AgentCore is bound")

    monkeypatch.setattr(login, "_loopback_open", fail_open)

    assert login._sealed_passkey(None) == {
        "ok": False,
        "loggedIn": False,
        "error": "in-app browser is not available",
    }
    assert login._in_app_http_origin("wss://cloud.example/cdp") is None
