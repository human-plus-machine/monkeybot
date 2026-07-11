"""Monkeybot chat TUI themes (dark / light)."""

from __future__ import annotations

import os

from textual.theme import Theme

THEME_DARK = "monkeybot-dark"
THEME_LIGHT = "monkeybot-light"

_DARK_VARS = {
    "muted": "#71717a",
    "disabled": "#52525b",
    "border": "#2a2f3a",
    "scrollbar": "#3d4450",
    "hitl-surface": "#1a1810",
    "hitl-text": "#fde68a",
    "assistant": "#d4d4d8",
    "tool-error": "#a16207",
}

_LIGHT_VARS = {
    "muted": "#52525b",
    "disabled": "#a1a1aa",
    "border": "#d4d4d8",
    "scrollbar": "#a1a1aa",
    "hitl-surface": "#fef9c3",
    "hitl-text": "#854d0e",
    "assistant": "#3f3f46",
    "tool-error": "#a16207",
}

MONKEYBOT_DARK = Theme(
    name=THEME_DARK,
    primary="#5b7cfa",
    secondary="#a1a1aa",
    accent="#5b7cfa",
    foreground="#e4e4e7",
    background="#0f1115",
    surface="#151820",
    panel="#151820",
    success="#22c55e",
    warning="#eab308",
    error="#f87171",
    dark=True,
    variables=_DARK_VARS,
)

MONKEYBOT_LIGHT = Theme(
    name=THEME_LIGHT,
    primary="#4f6ef7",
    secondary="#52525b",
    accent="#4f6ef7",
    foreground="#18181b",
    background="#f4f4f5",
    surface="#ffffff",
    panel="#ffffff",
    success="#16a34a",
    warning="#ca8a04",
    error="#dc2626",
    dark=False,
    variables=_LIGHT_VARS,
)


def _colorfgbg_prefers_light() -> bool | None:
    """Return True/False if COLORFGBG indicates light/dark bg, else None."""
    raw = os.environ.get("COLORFGBG", "").strip()
    if not raw or ";" not in raw:
        return None
    # Format: foreground;background (decimal ANSI color indices).
    # Light backgrounds are typically 7/15 (white) or high-intensity.
    try:
        bg = int(raw.rsplit(";", 1)[-1])
    except ValueError:
        return None
    return bg in {7, 15}


def resolve_theme_name(choice: str = "auto") -> str:
    """Map ``auto|dark|light`` to a registered theme name."""
    normalized = (choice or "auto").strip().lower()
    if normalized == "light":
        return THEME_LIGHT
    if normalized == "dark":
        return THEME_DARK
    prefers_light = _colorfgbg_prefers_light()
    if prefers_light is True:
        return THEME_LIGHT
    return THEME_DARK
