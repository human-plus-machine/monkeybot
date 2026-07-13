"""Tests for chat TUI theme resolution."""

from __future__ import annotations

import re

from monkeybot_cli.chat_theme import (
    THEME_DARK,
    THEME_LIGHT,
    resolve_theme_name,
)
from monkeybot_cli.chat_tui import (
    AssistantTurn,
    ChatApp,
    EmptyHint,
    GroundingBlock,
    HitlCard,
    SystemLine,
    ThinkingLine,
    ToolCallBlock,
    UserTurn,
    EarlierTurns,
)

_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def test_resolve_theme_name_explicit() -> None:
    assert resolve_theme_name("dark") == THEME_DARK
    assert resolve_theme_name("light") == THEME_LIGHT
    assert resolve_theme_name("DARK") == THEME_DARK


def test_resolve_theme_name_auto_colorfgbg(monkeypatch) -> None:
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert resolve_theme_name("auto") == THEME_LIGHT
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert resolve_theme_name("auto") == THEME_DARK
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert resolve_theme_name("auto") == THEME_DARK


def test_chat_app_css_has_no_hex_literals() -> None:
    assert _HEX_RE.search(ChatApp.CSS) is None
    for cls in (
        ThinkingLine,
        SystemLine,
        GroundingBlock,
        EarlierTurns,
        UserTurn,
        AssistantTurn,
        ToolCallBlock,
        EmptyHint,
        HitlCard,
    ):
        css = getattr(cls, "DEFAULT_CSS", "")
        assert _HEX_RE.search(css) is None, f"{cls.__name__} still has hex colors"


def test_chat_app_registers_themes(tmp_path) -> None:
    app = ChatApp(
        base="http://127.0.0.1:9",
        agent_root=tmp_path,
        provider="fake",
        model="fake-model",
        spawned_gateway=False,
        theme_choice="light",
    )
    assert app.theme == THEME_LIGHT
    assert THEME_DARK in app.available_themes
    assert THEME_LIGHT in app.available_themes
