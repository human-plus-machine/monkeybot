"""Per-turn token and cost data types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Usage:
    """Aggregated usage for one model turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    estimated_prompt_tokens: int = 0
    """Peak pre-stream input token count for this user turn (``Provider.count_input_tokens``).

    Same payload as each outbound ``stream`` call (messages + tools), using the
    vendor count API or tokenizer where implemented.
    """


@dataclass(frozen=True)
class UsageSummary:
    """Aggregated totals returned by a usage store's ``summary`` method."""

    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    period_start_ms: int | None
    period_end_ms: int | None
    last_prompt_tokens: int
    last_estimated_prompt_tokens: int
