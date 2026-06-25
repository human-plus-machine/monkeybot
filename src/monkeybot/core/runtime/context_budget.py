"""Dynamic context-window budgeting for tool results (Option B)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse

_INVENTORY_MARKER = "[Spill inventory —"
_CHARS_PER_TOKEN = 4

_DIFF_GIT_RE = re.compile(r"^diff --git a/.+ b/(.+)$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    """Cheap local token estimate (no network)."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _text_from_blocks(blocks: list[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
    return "".join(parts)


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

    @classmethod
    def from_env(cls, *, window_tokens: int, used_tokens: int) -> ContextBudgeter:
        return cls(
            window_tokens=window_tokens,
            used_tokens=used_tokens,
            safety_fraction=budget_fraction_from_env(),
            floor_tokens=budget_floor_tokens_from_env(),
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

        out: list[ContentBlock] = []
        for block in blocks:
            if not isinstance(block, ToolResponse) or block.is_error:
                out.append(block)
                continue
            text = _text_from_blocks(list(block.result))
            est = estimate_tokens(text)
            if est <= per_item:
                out.append(block)
                self.used_tokens += est
                continue
            needs_compaction = True
            trimmed = _trim_text_to_token_budget(text, per_item)
            out.append(
                ToolResponse(
                    id=block.id,
                    tool_name=block.tool_name,
                    result=[Text(text=trimmed)],
                    is_error=block.is_error,
                )
            )
            self.used_tokens += estimate_tokens(trimmed)

        return out, needs_compaction

    def _trim_all(self, blocks: list[ContentBlock], per_item: int) -> list[ContentBlock]:
        out: list[ContentBlock] = []
        for block in blocks:
            if not isinstance(block, ToolResponse) or block.is_error:
                out.append(block)
                continue
            text = _text_from_blocks(list(block.result))
            trimmed = _trim_text_to_token_budget(text, per_item)
            out.append(
                ToolResponse(
                    id=block.id,
                    tool_name=block.tool_name,
                    result=[Text(text=trimmed)],
                    is_error=block.is_error,
                )
            )
            self.used_tokens += estimate_tokens(trimmed)
        return out


def diff_inventory_lines(text: str) -> list[str] | None:
    """Return changed paths from unified diff headers, or None if not a diff."""
    paths = _DIFF_GIT_RE.findall(text)
    if not paths:
        return None
    return paths
