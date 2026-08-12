"""In-app CDP preference + daemon bounce for Monkeyapp's embedded Chromium."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

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
    # Ignore a developer machine's live MonkeyApp CDP file.
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", tmp_path / "missing-in-app-cdp-url")
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

    assert server._apply_in_app_cdp_url() == "http://127.0.0.1:9333"
    assert server.os.environ.get("BU_CDP_URL") == "http://127.0.0.1:9333"


def test_apply_in_app_cdp_url_reads_http_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("http://127.0.0.1:9333\n", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    assert server._apply_in_app_cdp_url() == "http://127.0.0.1:9333"
    assert server.os.environ.get("BU_CDP_URL") == "http://127.0.0.1:9333"


def test_apply_in_app_cdp_url_reads_ws_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_file = tmp_path / "in-app-cdp-url"
    cdp_file.write_text("ws://127.0.0.1:9333/devtools", encoding="utf-8")
    monkeypatch.setattr(server, "_IN_APP_CDP_URL_FILE", cdp_file)

    assert server._apply_in_app_cdp_url() == "ws://127.0.0.1:9333/devtools"
    assert server.os.environ.get("BU_CDP_WS") == "ws://127.0.0.1:9333/devtools"
    assert "BU_CDP_URL" not in server.os.environ


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

    assert server._apply_in_app_cdp_url() == "http://127.0.0.1:9333"
    assert server.os.environ.get("BU_CDP_URL") == "http://127.0.0.1:9333"

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

    assert server._apply_in_app_cdp_url() == "http://127.0.0.1:9333"

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
