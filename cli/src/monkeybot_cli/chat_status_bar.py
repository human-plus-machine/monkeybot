"""Context-window ring formatting for ``monkeybot chat``."""

from __future__ import annotations

from dataclasses import dataclass

from monkeybot.core.runtime.context_budget import SUMMARY_TRIGGER_RATIO

_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"

DEFAULT_CONTEXT_WINDOW = 200_000
_RING_GLYPHS = ("○", "◔", "◑", "◕", "●")


@dataclass(frozen=True)
class SessionUsageView:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    last_prompt_tokens: int = 0
    estimated_prompt_tokens: int = 0
    summarization_threshold_tokens: int = 0
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW


def parse_usage_response(data: dict[str, object]) -> SessionUsageView:
    def _int(key: str, default: int = 0) -> int:
        val = data.get(key, default)
        return int(val) if isinstance(val, (int, float)) else default

    def _float(key: str, default: float = 0.0) -> float:
        val = data.get(key, default)
        return float(val) if isinstance(val, (int, float)) else default

    cap = _int("context_window_tokens", DEFAULT_CONTEXT_WINDOW)
    thresh = _int("summarization_threshold_tokens", 0)
    if thresh <= 0:
        thresh = max(1, int(cap * SUMMARY_TRIGGER_RATIO))
    return SessionUsageView(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cost_usd=_float("cost_usd"),
        last_prompt_tokens=_int("last_prompt_tokens"),
        estimated_prompt_tokens=_int("estimated_prompt_tokens"),
        summarization_threshold_tokens=thresh,
        context_window_tokens=max(1, cap),
    )


def _ring_glyph(pct_used: int) -> str:
    idx = min(len(_RING_GLYPHS) - 1, max(0, (pct_used + 12) // 25))
    return _RING_GLYPHS[idx]


def _ring_color(ring_numerator: int, threshold: int) -> str:
    if ring_numerator >= threshold:
        return _RED
    if ring_numerator >= int(threshold * 0.75):
        return _YELLOW
    return _GREEN


def _ring_markup_color(ring_numerator: int, threshold: int) -> str:
    """Rich markup color name for the same thresholds as :func:`_ring_color`."""
    if ring_numerator >= threshold:
        return "red"
    if ring_numerator >= int(threshold * 0.75):
        return "yellow"
    return "green"


def _ring_parts(
    *,
    estimated_prompt_tokens: int,
    last_prompt_tokens: int,
    context_window_tokens: int,
    summarization_threshold_tokens: int,
) -> tuple[str, int, int, int]:
    """Return ``(glyph, pct_used, ring_numerator, thresh)``."""
    cap = max(1, context_window_tokens)
    ring_numerator = estimated_prompt_tokens if estimated_prompt_tokens > 0 else last_prompt_tokens
    thresh = max(
        1,
        summarization_threshold_tokens
        if summarization_threshold_tokens > 0
        else int(cap * SUMMARY_TRIGGER_RATIO),
    )
    pct_used = min(100, max(0, round((ring_numerator / cap) * 100)))
    return _ring_glyph(pct_used), pct_used, ring_numerator, thresh


def format_context_ring(
    *,
    estimated_prompt_tokens: int,
    last_prompt_tokens: int,
    context_window_tokens: int,
    summarization_threshold_tokens: int,
) -> str:
    glyph, pct_used, ring_numerator, thresh = _ring_parts(
        estimated_prompt_tokens=estimated_prompt_tokens,
        last_prompt_tokens=last_prompt_tokens,
        context_window_tokens=context_window_tokens,
        summarization_threshold_tokens=summarization_threshold_tokens,
    )
    color = _ring_color(ring_numerator, thresh)
    return f"{color}{glyph} {pct_used}%{_RESET}"


def format_context_ring_plain(usage: SessionUsageView | None) -> str:
    """Ring text without ANSI (for plain / non-color contexts)."""
    if usage is None:
        return "○ 0%"
    glyph, pct_used, _, _ = _ring_parts(
        estimated_prompt_tokens=usage.estimated_prompt_tokens,
        last_prompt_tokens=usage.last_prompt_tokens,
        context_window_tokens=usage.context_window_tokens,
        summarization_threshold_tokens=usage.summarization_threshold_tokens,
    )
    return f"{glyph} {pct_used}%"


def format_context_ring_markup(usage: SessionUsageView | None) -> str:
    """Ring text with Rich markup colors for the Textual status bar."""
    if usage is None:
        return "[dim]○ 0%[/]"
    glyph, pct_used, ring_numerator, thresh = _ring_parts(
        estimated_prompt_tokens=usage.estimated_prompt_tokens,
        last_prompt_tokens=usage.last_prompt_tokens,
        context_window_tokens=usage.context_window_tokens,
        summarization_threshold_tokens=usage.summarization_threshold_tokens,
    )
    color = _ring_markup_color(ring_numerator, thresh)
    return f"[{color}]{glyph} {pct_used}%[/]"


def format_status_line(usage: SessionUsageView | None, *, width: int) -> str:
    if usage is None:
        ring = f"{_DIM}○ 0%{_RESET}"
    else:
        ring = format_context_ring(
            estimated_prompt_tokens=usage.estimated_prompt_tokens,
            last_prompt_tokens=usage.last_prompt_tokens,
            context_window_tokens=usage.context_window_tokens,
            summarization_threshold_tokens=usage.summarization_threshold_tokens,
        )
    line = f" {ring}"
    visible_budget = max(20, width - 1)
    return line[:visible_budget]


class UsageStore:
    """Holds session usage for footer / plain status (no DECSTBM)."""

    def __init__(self) -> None:
        self._usage: SessionUsageView | None = None

    @property
    def usage(self) -> SessionUsageView | None:
        return self._usage

    def update(self, usage: SessionUsageView) -> None:
        self._usage = usage

    def update_context_hint(
        self,
        *,
        estimated_prompt_tokens: int,
        context_window_tokens: int = 0,
        summarization_threshold_tokens: int = 0,
    ) -> None:
        base = self._usage or SessionUsageView()
        cap = context_window_tokens if context_window_tokens > 0 else base.context_window_tokens
        thresh = (
            summarization_threshold_tokens
            if summarization_threshold_tokens > 0
            else base.summarization_threshold_tokens
        )
        self._usage = SessionUsageView(
            input_tokens=base.input_tokens,
            output_tokens=base.output_tokens,
            cost_usd=base.cost_usd,
            last_prompt_tokens=base.last_prompt_tokens,
            estimated_prompt_tokens=estimated_prompt_tokens,
            summarization_threshold_tokens=thresh,
            context_window_tokens=cap,
        )


def format_voice_status(state: str, level_db: float | None = None) -> str:
    """Rich markup voice indicator for the chat status bar."""
    labels = {
        "listening": "[green]● listening[/]",
        "muted": "[dim]◌ muted[/]",
        "ptt_held": "[cyan]⏺ PTT[/]",
        "speaking": "[magenta]▶ speaking[/]",
    }
    base = labels.get(state, f"[dim]{state}[/]")
    if level_db is None:
        return base
    # Map -40..0 dBFS to 0..4 bars
    bars = max(0, min(4, int((level_db + 40) / 10)))
    meter = "▁▂▃▄▅"[bars] if bars < 5 else "▅"
    return f"{base} [dim]{meter}[/]"
