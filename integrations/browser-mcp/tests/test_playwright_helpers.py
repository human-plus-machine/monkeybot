"""playwright_helpers.py unit tests that don't require a real AgentCore/browser
connection -- exercise tab-id handling and reconnect-on-stale-connection retry
logic directly against the module's internal state (MagicMock stand-ins for
Playwright objects; no thread-affinity constraint applies to those).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from browser_mcp import playwright_helpers as ph


@pytest.fixture(autouse=True)
def _reset_state():
    original_state = dict(ph._state)
    original_tab_ids = dict(ph._tab_ids)
    original_hook = ph._reconnect_hook
    yield
    ph._state.clear()
    ph._state.update(original_state)
    ph._tab_ids.clear()
    ph._tab_ids.update(original_tab_ids)
    ph.set_reconnect_hook(original_hook)


# --- switch_tab ---


def test_switch_tab_unknown_numeric_id_raises_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="unknown target_id"):
        ph.switch_tab("12345")


def test_switch_tab_non_numeric_id_raises_runtime_error_not_value_error() -> None:
    """Regression: int(target_id) must not leak a raw ValueError past the
    RuntimeError contract every other playwright_helpers function follows."""
    with pytest.raises(RuntimeError, match="unknown target_id"):
        ph.switch_tab("not-a-number")


def test_switch_tab_switches_to_known_page() -> None:
    page = MagicMock()
    ph._tab_ids[id(page)] = page

    result = ph.switch_tab(str(id(page)))

    page.bring_to_front.assert_called_once()
    assert ph._state["page"] is page
    assert result == str(id(page))


# --- list_tabs pruning ---


def test_list_tabs_prunes_stale_tab_ids() -> None:
    """A tab_id from a since-closed page must not linger in _tab_ids forever."""
    ph._tab_ids[999999] = MagicMock()  # simulates a closed tab from a prior call

    live_page = MagicMock()
    live_page.url = "https://example.com"
    live_page.title.return_value = "Example"
    context = MagicMock()
    context.pages = [live_page]
    ph._state["context"] = context

    out = ph.list_tabs(include_chrome=True)

    assert 999999 not in ph._tab_ids
    assert ph._tab_ids[id(live_page)] is live_page
    assert out == [
        {
            "targetId": str(id(live_page)),
            "target_id": str(id(live_page)),
            "title": "Example",
            "url": "https://example.com",
        }
    ]


def test_list_tabs_requires_connection() -> None:
    ph._state["context"] = None
    with pytest.raises(RuntimeError, match="not connected"):
        ph.list_tabs()


# --- reconnect-on-stale-session retry ---


def test_run_retries_once_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("playwright")
    from playwright.sync_api import Error as PlaywrightError

    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise PlaywrightError("Target page, context or browser has been closed")
        return "ok"

    hook_calls: list[int] = []

    def hook() -> tuple[str, dict[str, str]]:
        hook_calls.append(1)
        return ("wss://new/ws", {"Authorization": "x"})

    ph.set_reconnect_hook(hook)
    fake_connect = MagicMock()
    monkeypatch.setattr(ph, "_connect_impl", fake_connect)

    result = ph._run(flaky)

    assert result == "ok"
    assert len(calls) == 2
    assert len(hook_calls) == 1
    fake_connect.assert_called_once_with("wss://new/ws", {"Authorization": "x"})


def test_run_reraises_connection_error_without_reconnect_hook() -> None:
    pytest.importorskip("playwright")
    from playwright.sync_api import Error as PlaywrightError

    ph.set_reconnect_hook(None)

    def boom() -> None:
        raise PlaywrightError("Target page, context or browser has been closed")

    with pytest.raises(PlaywrightError):
        ph._run(boom)


def test_run_does_not_retry_non_connection_errors() -> None:
    """An ordinary failure (e.g. element not found) must not trigger a
    reconnect -- only connection-death errors should."""
    hook = MagicMock(return_value=("wss://new/ws", {}))
    ph.set_reconnect_hook(hook)

    def boom() -> None:
        raise RuntimeError("element not found")

    with pytest.raises(RuntimeError, match="element not found"):
        ph._run(boom)
    hook.assert_not_called()
