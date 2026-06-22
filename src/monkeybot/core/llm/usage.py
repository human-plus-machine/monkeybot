"""Per-turn token and cost data types."""

from __future__ import annotations

from dataclasses import dataclass

from monkeybot.core.runtime.events import UsageTotals


@dataclass
class Usage:
    """Aggregated usage for one model turn."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    estimated_prompt_tokens: int = 0
    """Peak pre-stream input token count for this user turn (``Provider.count_input_tokens``).

    Same payload as each outbound ``stream`` call (messages + tools), using the
    vendor count API or tokenizer where implemented.
    """


def usage_from_totals(t: UsageTotals) -> Usage:
    """Build a :class:`Usage` row from :class:`~monkeybot.core.runtime.events.UsageTotals` (e.g. ``TurnComplete.usage``)."""
    return Usage(
        input_tokens=t.input_tokens,
        output_tokens=t.output_tokens,
        cached_tokens=t.cached_tokens,
        cache_read_tokens=t.cache_read_tokens,
        cache_creation_tokens=t.cache_creation_tokens,
        cost_usd=t.cost_usd,
        duration_ms=t.duration_ms,
        estimated_prompt_tokens=t.estimated_prompt_tokens,
    )


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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
