"""Parity checks for ChatRenderer / EVENT_KINDS coverage."""

from __future__ import annotations

from monkeybot_cli.chat_renderer import EVENT_KINDS, ChatRenderer
from monkeybot_cli.chat_tui import _TUI_EVENT_HANDLERS
from monkeybot_cli.commands.chat import _PlainRenderer


def test_tui_handlers_cover_all_event_kinds() -> None:
    missing = EVENT_KINDS - frozenset(_TUI_EVENT_HANDLERS)
    extra = frozenset(_TUI_EVENT_HANDLERS) - EVENT_KINDS
    assert not missing, f"TUI missing handlers for: {sorted(missing)}"
    assert not extra, f"TUI has unknown kinds: {sorted(extra)}"


def test_plain_handlers_cover_all_event_kinds() -> None:
    plain = _PlainRenderer()
    missing = [
        kind for kind in sorted(EVENT_KINDS) if not hasattr(plain, f"_on_{kind}")
    ]
    assert not missing, f"Plain renderer missing _on_* for: {missing}"


def test_plain_renderer_satisfies_protocol() -> None:
    assert isinstance(_PlainRenderer(), ChatRenderer)
