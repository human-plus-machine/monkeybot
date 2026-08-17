"""Dynamic context-window budgeting for tool results (Option B)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from monkeybot.core.context.common import ContextPressureTier, text_from_blocks
from monkeybot.core.context.tool_output_policy import resolve_tool_budget
from monkeybot.core.context.tool_result_ingress import (
    sanitize_tool_result_text,
    skip_tool_result_sanitize,
)
from monkeybot.core.context.tool_shapers import shape_tool_text
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse

logger = logging.getLogger(__name__)

_INVENTORY_MARKER = "[Spill inventory —"
_CHARS_PER_TOKEN = 4

_DIFF_GIT_RE = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)

# Fixed harness policy — not env/YAML tunable.
PRESSURE_LIGHT_RATIO = 0.50
PRESSURE_MODERATE_RATIO = 0.70
PRESSURE_AGGRESSIVE_RATIO = 0.85
# Sync history summarization fires at the same bar as aggressive tool-result
# shaping — row count is otherwise unbounded until this token bar is hit.
SUMMARY_TRIGGER_RATIO = PRESSURE_AGGRESSIVE_RATIO
RESULT_BUDGET_FRACTION = 0.8
RESULT_BUDGET_FLOOR_TOKENS = 2000


def compute_context_pressure_tier(
    used_tokens: int,
    window_tokens: int,
    *,
    light_ratio: float = PRESSURE_LIGHT_RATIO,
    moderate_ratio: float = PRESSURE_MODERATE_RATIO,
    aggressive_ratio: float = PRESSURE_AGGRESSIVE_RATIO,
) -> ContextPressureTier | None:
    """Return pressure tier from ``used_tokens / window_tokens``, or None when low."""
    if window_tokens <= 0:
        return None
    ratio = used_tokens / window_tokens
    if ratio >= aggressive_ratio:
        return "aggressive"
    if ratio >= moderate_ratio:
        return "moderate"
    if ratio >= light_ratio:
        return "light"
    return None


def _safety_fraction_for_tier(
    base_fraction: float,
    tier: ContextPressureTier | None,
) -> float:
    if tier == "light":
        return min(base_fraction, 0.65)
    if tier == "moderate":
        return min(base_fraction, 0.45)
    if tier == "aggressive":
        return min(base_fraction, 0.25)
    return base_fraction


def estimate_tokens_from_char_count(char_count: int) -> int:
    """Cheap local token estimate from a raw character count (no network)."""
    if char_count <= 0:
        return 0
    return max(1, char_count // _CHARS_PER_TOKEN)


def estimate_tokens(text: str) -> int:
    """Cheap local token estimate (no network)."""
    return estimate_tokens_from_char_count(len(text))


def _tool_response_with_text(block: ToolResponse, text: str) -> ToolResponse:
    original = text_from_blocks(list(block.result))
    if text == original:
        return block
    return ToolResponse(
        id=block.id,
        tool_name=block.tool_name,
        result=[Text(text=text)],
        is_error=block.is_error,
    )


def _split_inventory_note(text: str) -> tuple[str, str | None]:
    """Return (body, inventory_note) when an inventory block is present."""
    idx = text.rfind(_INVENTORY_MARKER)
    if idx == -1:
        return text, None
    return text[:idx].rstrip(), text[idx:].lstrip()


def _trim_text_to_token_budget(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    max_chars = max(1, token_budget * _CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    body, note = _split_inventory_note(text)
    if note:
        note_len = len(note) + 1
        head_budget = max(0, max_chars - note_len)
        if head_budget <= 0:
            return note
        trimmed_body = body[:head_budget]
        if len(body) > head_budget:
            trimmed_body += "\n[... truncated for context budget — use read_file on spill path to page ...]"
        return f"{trimmed_body}\n{note}"
    trimmed = text[:max_chars]
    if len(text) > max_chars:
        trimmed += "\n[... truncated for context budget — use read_file on spill path to page ...]"
    return trimmed


@dataclass
class ContextBudgeter:
    """Allocate remaining context headroom across a batch of tool results."""

    window_tokens: int
    used_tokens: int
    safety_fraction: float = RESULT_BUDGET_FRACTION
    floor_tokens: int = RESULT_BUDGET_FLOOR_TOKENS
    pressure_tier: ContextPressureTier | None = None

    @classmethod
    def for_window(cls, *, window_tokens: int, used_tokens: int) -> ContextBudgeter:
        tier = compute_context_pressure_tier(used_tokens, window_tokens)
        return cls(
            window_tokens=window_tokens,
            used_tokens=used_tokens,
            safety_fraction=_safety_fraction_for_tier(RESULT_BUDGET_FRACTION, tier),
            floor_tokens=RESULT_BUDGET_FLOOR_TOKENS,
            pressure_tier=tier,
        )

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.window_tokens - self.used_tokens)

    def fit_content_blocks(
        self,
        blocks: list[ContentBlock],
    ) -> tuple[list[ContentBlock], bool]:
        """Trim tool-response blocks to fit remaining headroom; return (blocks, needs_compaction)."""
        if not blocks:
            return blocks, False

        remaining = self.remaining_tokens
        if remaining <= self.floor_tokens:
            return self._trim_all(blocks, max(1, remaining // max(1, len(blocks)))), True

        pool = max(1, int(remaining * self.safety_fraction))
        per_item = max(1, pool // len(blocks))
        needs_compaction = remaining < self.floor_tokens * 2
        if self.pressure_tier in ("moderate", "aggressive"):
            needs_compaction = True

        out: list[ContentBlock] = []
        for block in blocks:
            shaped = self._shape_tool_response(block)
            if shaped is None:
                out.append(block)
                continue
            block, text = shaped
            est = estimate_tokens(text)
            if est <= per_item:
                out.append(_tool_response_with_text(block, text))
                self.used_tokens += est
                continue
            needs_compaction = True
            trimmed = _trim_text_to_token_budget(text, per_item)
            out.append(_tool_response_with_text(block, trimmed))
            self.used_tokens += estimate_tokens(trimmed)

        return out, needs_compaction

    def _shape_tool_response(self, block: ContentBlock) -> tuple[ToolResponse, str] | None:
        if not isinstance(block, ToolResponse) or block.is_error:
            return None
        text = text_from_blocks(list(block.result))
        if not skip_tool_result_sanitize(block.tool_name):
            text = sanitize_tool_result_text(text)
        body, note = _split_inventory_note(text)
        if note is not None:
            # Soft-spilled results were sized at ingress; only reshape under pressure
            # and never drop the inventory pointer.
            if self.pressure_tier in ("moderate", "aggressive"):
                budget = resolve_tool_budget(block.tool_name)
                shaped_body = shape_tool_text(
                    body,
                    tool_name=block.tool_name,
                    budget=budget,
                    pressure_tier=self.pressure_tier,
                )
                text = f"{shaped_body}\n{note}" if shaped_body else note
            else:
                text = f"{body}\n{note}" if body else note
            return block, text
        budget = resolve_tool_budget(block.tool_name)
        if budget is not None or self.pressure_tier in ("moderate", "aggressive"):
            text = shape_tool_text(
                text,
                tool_name=block.tool_name,
                budget=budget,
                pressure_tier=self.pressure_tier,
            )
        return block, text

    def _trim_all(self, blocks: list[ContentBlock], per_item: int) -> list[ContentBlock]:
        out: list[ContentBlock] = []
        for block in blocks:
            shaped = self._shape_tool_response(block)
            if shaped is None:
                out.append(block)
                continue
            block, text = shaped
            trimmed = _trim_text_to_token_budget(text, per_item)
            out.append(_tool_response_with_text(block, trimmed))
            self.used_tokens += estimate_tokens(trimmed)
        return out


def diff_inventory_lines(text: str) -> list[str] | None:
    """Return changed paths from unified diff headers, or None if not a diff."""
    paths = _DIFF_GIT_RE.findall(text)
    if not paths:
        return None
    return paths
