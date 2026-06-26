"""Pinned bottom status bar for ``monkeybot chat`` (context ring + session usage)."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

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
        thresh = max(1, int(cap * 0.85))
    return SessionUsageView(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cost_usd=_float("cost_usd"),
        last_prompt_tokens=_int("last_prompt_tokens"),
        estimated_prompt_tokens=_int("estimated_prompt_tokens"),
        summarization_threshold_tokens=thresh,
        context_window_tokens=max(1, cap),
    )


def _format_tokens(n: int) -> str:
    return f"{n:,}"


def _format_usd(n: float) -> str:
    if not n or n <= 0:
        return "$0.00"
    return f"${n:.4f}"


def _ring_glyph(pct_used: int) -> str:
    idx = min(len(_RING_GLYPHS) - 1, max(0, (pct_used + 12) // 25))
    return _RING_GLYPHS[idx]


def _ring_color(ring_numerator: int, threshold: int) -> str:
    if ring_numerator >= threshold:
        return _RED
    if ring_numerator >= int(threshold * 0.75):
        return _YELLOW
    return _GREEN


def format_context_ring(
    *,
    estimated_prompt_tokens: int,
    last_prompt_tokens: int,
    context_window_tokens: int,
    summarization_threshold_tokens: int,
) -> str:
    cap = max(1, context_window_tokens)
    ring_numerator = estimated_prompt_tokens if estimated_prompt_tokens > 0 else last_prompt_tokens
    thresh = max(
        1,
        summarization_threshold_tokens
        if summarization_threshold_tokens > 0
        else int(cap * 0.85),
    )
    pct_used = min(100, max(0, round((ring_numerator / cap) * 100)))
    glyph = _ring_glyph(pct_used)
    color = _ring_color(ring_numerator, thresh)
    return f"{color}{glyph} {pct_used}%{_RESET}"


def format_status_line(usage: SessionUsageView | None, *, width: int) -> str:
    if usage is None:
        ring = f"{_DIM}○ 0%{_RESET}"
        stats = f"{_DIM}In —  Out —  $0.00{_RESET}"
    else:
        ring = format_context_ring(
            estimated_prompt_tokens=usage.estimated_prompt_tokens,
            last_prompt_tokens=usage.last_prompt_tokens,
            context_window_tokens=usage.context_window_tokens,
            summarization_threshold_tokens=usage.summarization_threshold_tokens,
        )
        stats = (
            f"{_DIM}In {_format_tokens(usage.input_tokens)}  "
            f"Out {_format_tokens(usage.output_tokens)}  "
            f"{_format_usd(usage.cost_usd)}{_RESET}"
        )
    line = f" {ring}  {stats}"
    visible_budget = max(20, width - 1)
    if len(_strip_ansi(line)) > visible_budget:
        compact = (
            f" {ring}  {_DIM}{_format_tokens(usage.input_tokens if usage else 0)}"
            f"/{_format_tokens(usage.output_tokens if usage else 0)}  "
            f"{_format_usd(usage.cost_usd if usage else 0.0)}{_RESET}"
        )
        line = compact
    return line[:visible_budget]


def _strip_ansi(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "[":
            j = i + 2
            while j < len(text) and not text[j].isalpha():
                j += 1
            i = j + 1 if j < len(text) else len(text)
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _terminal_rows() -> int:
    return max(2, shutil.get_terminal_size(fallback=(80, 24)).lines)


class ChatStatusBar:
    """Renders session usage on a dedicated bottom terminal row (TTY only).

    Uses DECSTBM to keep chat output in lines 1..rows-1 so the status bar never
    scrolls away with conversation text.
    """

    def __init__(self) -> None:
        self._usage: SessionUsageView | None = None
        self._active = False
        self._bottom_row = 0

    @property
    def usage(self) -> SessionUsageView | None:
        return self._usage

    @property
    def active(self) -> bool:
        return self._active

    def activate(self) -> None:
        if not sys.stdout.isatty() or self._active:
            return
        rows = _terminal_rows()
        self._bottom_row = rows
        # Chat scrolls on lines 1..rows-1; bottom row is reserved for the status bar.
        sys.stdout.write(f"\x1b[1;{rows - 1}r")
        sys.stdout.write(f"\x1b[{rows - 1};1H")
        sys.stdout.flush()
        self._active = True
        self.render()

    def deactivate(self) -> None:
        if not sys.stdout.isatty() or not self._active:
            return
        rows = _terminal_rows()
        self._write_bottom_line(rows, "")
        sys.stdout.write("\x1b[r")
        sys.stdout.flush()
        self._active = False
        self._bottom_row = 0

    def update(self, usage: SessionUsageView) -> None:
        self._usage = usage
        self.render()

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
        self.render()

    def render(self) -> None:
        if not sys.stdout.isatty():
            return
        if not self._active:
            return
        rows = self._bottom_row or _terminal_rows()
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        line = format_status_line(self._usage, width=cols)
        self._write_bottom_line(rows, line)

    def _write_bottom_line(self, row: int, text: str) -> None:
        sys.stdout.write("\x1b7")
        sys.stdout.write(f"\x1b[{row};1H\x1b[2K{text}")
        sys.stdout.write("\x1b8")
        sys.stdout.flush()
