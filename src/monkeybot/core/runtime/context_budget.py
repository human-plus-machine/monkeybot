"""Dynamic context-window budgeting for tool results (Option B)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from monkeybot.core.context.common import ContextPressureTier, text_from_blocks
from monkeybot.core.context.tool_output_policy import resolve_tool_budget
from monkeybot.core.context.tool_shapers import shape_tool_text
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse

logger = logging.getLogger(__name__)

_INVENTORY_MARKER = "[Spill inventory —"
_CHARS_PER_TOKEN = 4

_DIFF_GIT_RE = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)

_DEFAULT_LIGHT_RATIO = 0.50
_DEFAULT_MODERATE_RATIO = 0.70
_DEFAULT_AGGRESSIVE_RATIO = 0.85


def _ratio_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "invalid env var value %s",
            kv(name=name, value=raw, default=default),
        )
        return default
    return min(0.99, max(0.05, val))


def pressure_light_ratio_from_env() -> float:
    return _ratio_from_env("MONKEYBOT_PRESSURE_LIGHT_RATIO", _DEFAULT_LIGHT_RATIO)


def pressure_moderate_ratio_from_env() -> float:
    return _ratio_from_env("MONKEYBOT_PRESSURE_MODERATE_RATIO", _DEFAULT_MODERATE_RATIO)


def pressure_aggressive_ratio_from_env() -> float:
    return _ratio_from_env("MONKEYBOT_PRESSURE_AGGRESSIVE_RATIO", _DEFAULT_AGGRESSIVE_RATIO)


def summarization_trigger_ratio_from_env() -> float:
    """Ratio of context window that triggers full LLM summarization."""
    return pressure_aggressive_ratio_from_env()


def compute_context_pressure_tier(
    used_tokens: int,
    window_tokens: int,
    *,
    light_ratio: float | None = None,
    moderate_ratio: float | None = None,
    aggressive_ratio: float | None = None,
) -> ContextPressureTier | None:
    """Return pressure tier from ``used_tokens / window_tokens``, or None when low."""
    if window_tokens <= 0:
        return None
    ratio = used_tokens / window_tokens
    light = light_ratio if light_ratio is not None else pressure_light_ratio_from_env()
    moderate = moderate_ratio if moderate_ratio is not None else pressure_moderate_ratio_from_env()
    aggressive = (
        aggressive_ratio if aggressive_ratio is not None else pressure_aggressive_ratio_from_env()
    )
    if ratio >= aggressive:
        return "aggressive"
    if ratio >= moderate:
        return "moderate"
    if ratio >= light:
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


def estimate_tokens(text: str) -> int:
    """Cheap local token estimate (no network)."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


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


def budget_fraction_from_env() -> float:
    raw = os.environ.get("MONKEYBOT_RESULT_BUDGET_FRACTION", "0.8").strip()
    try:
        val = float(raw)
    except ValueError:
        return 0.8
    return min(1.0, max(0.05, val))


def budget_floor_tokens_from_env() -> int:
    raw = os.environ.get("MONKEYBOT_RESULT_BUDGET_FLOOR_TOKENS", "2000").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 2000


@dataclass
class ContextBudgeter:
    """Allocate remaining context headroom across a batch of tool results."""

    window_tokens: int
    used_tokens: int
    safety_fraction: float = 0.8
    floor_tokens: int = 2000
    pressure_tier: ContextPressureTier | None = None

    @classmethod
    def from_env(cls, *, window_tokens: int, used_tokens: int) -> ContextBudgeter:
        tier = compute_context_pressure_tier(used_tokens, window_tokens)
        base_fraction = budget_fraction_from_env()
        return cls(
            window_tokens=window_tokens,
            used_tokens=used_tokens,
            safety_fraction=_safety_fraction_for_tier(base_fraction, tier),
            floor_tokens=budget_floor_tokens_from_env(),
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
