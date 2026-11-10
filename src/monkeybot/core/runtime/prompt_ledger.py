"""Estimate the next prompt size from the last provider-reported count.

A real count runs on the first call of a request, after a history or tool-list
change, and whenever the estimate cannot say which side of a pressure bar the
prompt is on. Every accepted response replaces the anchor, so the error covers
one turn of new text and cannot accumulate.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from monkeybot.core.llm.provider import Message
from monkeybot.core.runtime.context_budget import (
    PRESSURE_AGGRESSIVE_RATIO,
    PRESSURE_LIGHT_RATIO,
    PRESSURE_MODERATE_RATIO,
    PROMPT_ANCHOR_MAX_DELTA_FRACTION,
    PROMPT_ANCHOR_TRUST_HI,
    PROMPT_ANCHOR_TRUST_LO,
    PROMPT_ESTIMATE_ABSOLUTE_SLACK,
    PROMPT_ESTIMATE_CHARS_PER_TOKEN,
    PROMPT_ESTIMATE_DELTA_HI,
    PROMPT_ESTIMATE_DELTA_LO,
)
from monkeybot.core.types.types_tools import ToolDef

_TIER_RATIOS = (
    PRESSURE_LIGHT_RATIO,
    PRESSURE_MODERATE_RATIO,
    PRESSURE_AGGRESSIVE_RATIO,
)


def request_chars(messages: Sequence[Message], tools: Sequence[ToolDef]) -> int:
    """Character size of one provider request. ``len`` work, no tokenizer."""
    from monkeybot.core.runtime.history_compaction import _raw_message_char_count

    total = sum(_raw_message_char_count(message) for message in messages)
    if tools:
        payload = [tool.to_model_schema() for tool in tools]
        total += len(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return total


def _tools_hash(tools: Sequence[ToolDef]) -> str:
    payload = [tool.to_model_schema() for tool in tools]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, kw_only=True)
class PromptSizeDecision:
    """Whether the next preflight can skip ``count_input_tokens``."""

    tokens: int
    needs_count: bool
    lo: int
    hi: int


class PromptTokenLedger:
    """One request's prompt-size anchor and the band around the next estimate."""

    def __init__(self) -> None:
        self.anchor_tokens: int | None = None
        self.anchor_output_tokens: int = 0
        self.anchor_chars: int = 0
        self.trusted: bool = False
        self.last_preflight_was_real: bool = False
        self._anchor_tools: str = ""
        self._pending_real: int | None = None
        self._pending_lo: int | None = None
        self._pending_hi: int | None = None

    def drop_if_tools_changed(self, tools: Sequence[ToolDef]) -> None:
        """Drop the anchor when the tool list is no longer the one that was counted."""
        if self.trusted and _tools_hash(tools) != self._anchor_tools:
            self.invalidate()

    def invalidate(self) -> None:
        """Drop the anchor. The next preflight counts for real."""
        self.anchor_tokens = None
        self.anchor_output_tokens = 0
        self.anchor_chars = 0
        self.trusted = False
        self._anchor_tools = ""
        self._pending_real = None
        self._pending_lo = None
        self._pending_hi = None

    def consider(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        window_tokens: int,
    ) -> PromptSizeDecision:
        """Return an estimate, or ask for a real count when the anchor cannot be used."""
        self.last_preflight_was_real = False
        self._pending_real = None
        self._pending_lo = None
        self._pending_hi = None
        anchor = self.anchor_tokens
        if (
            not self.trusted
            or anchor is None
            or window_tokens <= 0
            or _tools_hash(tools) != self._anchor_tools
        ):
            return PromptSizeDecision(tokens=0, needs_count=True, lo=0, hi=0)
        delta_est = (request_chars(messages, tools) - self.anchor_chars) / (
            PROMPT_ESTIMATE_CHARS_PER_TOKEN
        )
        if abs(delta_est) > PROMPT_ANCHOR_MAX_DELTA_FRACTION * anchor:
            return PromptSizeDecision(tokens=0, needs_count=True, lo=0, hi=0)
        if delta_est >= 0:
            lo = anchor + delta_est * PROMPT_ESTIMATE_DELTA_LO
            hi = anchor + delta_est * PROMPT_ESTIMATE_DELTA_HI
        else:
            lo = anchor + delta_est * PROMPT_ESTIMATE_DELTA_HI
            hi = anchor + delta_est * PROMPT_ESTIMATE_DELTA_LO
        lo -= PROMPT_ESTIMATE_ABSOLUTE_SLACK
        hi += PROMPT_ESTIMATE_ABSOLUTE_SLACK
        lo_i = max(0, math.floor(lo))
        hi_i = max(lo_i, math.ceil(hi))
        point_i = max(0, int(round(anchor + delta_est)))
        if _band_contains_tier_bar(lo_i, hi_i, window_tokens):
            return PromptSizeDecision(tokens=0, needs_count=True, lo=lo_i, hi=hi_i)
        return PromptSizeDecision(tokens=point_i, needs_count=False, lo=lo_i, hi=hi_i)

    def note_real_count(self, tokens: int) -> None:
        """Remember a count that was just performed, for the trust check on the reply."""
        self.last_preflight_was_real = True
        self._pending_real = tokens
        self._pending_lo = None
        self._pending_hi = None

    def note_estimate(self, decision: PromptSizeDecision) -> None:
        """Remember the band a skipped count committed to, for the self-check."""
        self.last_preflight_was_real = False
        self._pending_real = None
        self._pending_lo = decision.lo
        self._pending_hi = decision.hi

    def observe(
        self,
        *,
        prompt_tokens: int,
        output_tokens: int,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
    ) -> None:
        """Accept the provider's prompt size when it agrees with the preflight.

        A missing report, a real count the report disagrees with, or an estimate
        the report falls outside of, drops the anchor for the rest of the request
        until a later real count is confirmed.
        """
        if prompt_tokens < 1:
            self.invalidate()
            return
        if self._pending_real is not None:
            counted = self._pending_real
            if counted < 1 or not _within_trust(prompt_tokens, counted):
                self.invalidate()
                return
            self._accept(prompt_tokens, output_tokens, messages, tools)
            return
        if self._pending_lo is not None and self._pending_hi is not None:
            if prompt_tokens < self._pending_lo or prompt_tokens > self._pending_hi:
                self.invalidate()
                return
            self._accept(prompt_tokens, output_tokens, messages, tools)
            return
        self.invalidate()

    def post_batch_tokens(self, window_tokens: int) -> int | None:
        """Prompt size plus the reply just produced, when no tier bar sits between them.

        ``None`` means the caller should count the history for real.
        """
        if not self.trusted or self.anchor_tokens is None or window_tokens <= 0:
            return None
        low = self.anchor_tokens
        high = self.anchor_tokens + max(0, self.anchor_output_tokens)
        if _opens_across_tier_bar(min(low, high), max(low, high), window_tokens):
            return None
        return high

    def _accept(
        self,
        prompt_tokens: int,
        output_tokens: int,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
    ) -> None:
        self.anchor_tokens = prompt_tokens
        self.anchor_output_tokens = max(0, output_tokens)
        self.anchor_chars = request_chars(messages, tools)
        self._anchor_tools = _tools_hash(tools)
        self.trusted = True
        self._pending_real = None
        self._pending_lo = None
        self._pending_hi = None


def _within_trust(reported: int, counted: int) -> bool:
    return counted * PROMPT_ANCHOR_TRUST_LO <= reported <= counted * PROMPT_ANCHOR_TRUST_HI


def _band_contains_tier_bar(lo: int, hi: int, window_tokens: int) -> bool:
    """True when a pressure bar lies inside the closed interval ``lo..hi``."""
    for ratio in _TIER_RATIOS:
        bar = window_tokens * ratio
        if lo <= bar <= hi:
            return True
    return False


def _opens_across_tier_bar(low: int, high: int, window_tokens: int) -> bool:
    """True when ``low`` and ``high`` are on different sides of a pressure bar."""
    for ratio in _TIER_RATIOS:
        bar = window_tokens * ratio
        if low < bar <= high:
            return True
    return False
