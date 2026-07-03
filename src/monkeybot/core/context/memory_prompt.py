"""Memory index selection for the system prompt: window, curator, and hybrid modes."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

from monkeybot.core.context import TurnContext
from monkeybot.core.context.curator import (
    CuratedPromptParts,
    curation_enabled_from_env,
    curation_threshold_met,
    curator_model_id,
    memory_index_token_estimate,
    run_context_curator,
)
from monkeybot.core.llm.provider import Provider
from monkeybot.core.memory.index_format import memory_window_slice

_log = logging.getLogger(__name__)

# thread_id -> (index_fingerprint, curated_lines)
_curation_cache: dict[str, tuple[str, list[str]]] = {}


def reset_curation_cache_for_tests() -> None:
    _curation_cache.clear()


@dataclass(frozen=True)
class MemoryPromptSelection:
    """Memory lines and structural coverage for volatile system-prompt injection."""

    lines: list[str]
    total_lines: int
    coverage: float
    confidence: float
    nudge_search: bool
    use_custom_lines: bool


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def curation_mode_from_env() -> str:
    mode = os.getenv("CONTEXT_CURATION_MODE", "hybrid").strip().lower()
    if mode in ("window", "curator", "hybrid"):
        return mode
    return "hybrid"


def memory_window_lines_from_env() -> int:
    return max(1, _env_int("CONTEXT_CURATION_MEMORY_WINDOW_LINES", 12))


def memory_index_cap_from_env() -> int:
    return max(1, _env_int("MEMORY_INDEX_CAP", 200))


def memory_index_fingerprint(lines: list[str]) -> str:
    """Stable hash of index lines; used to detect mid-turn INDEX refreshes."""
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def memory_coverage(injected_count: int, total_count: int) -> float:
    if total_count <= 0:
        return 1.0
    return min(1.0, injected_count / total_count)


def memory_confidence(*, coverage: float, truncated: bool) -> float:
    """Structural confidence (not LLM-reported). Full index → 1.0; else equals coverage."""
    if not truncated:
        return 1.0
    return round(coverage, 4)


def _selection_from_lines(
    injected: list[str],
    total: list[str],
    *,
    use_custom_lines: bool,
) -> MemoryPromptSelection:
    total_n = len(total)
    injected_n = len(injected)
    truncated = injected_n < total_n
    coverage = memory_coverage(injected_n, total_n)
    confidence = memory_confidence(coverage=coverage, truncated=truncated)
    return MemoryPromptSelection(
        lines=list(injected),
        total_lines=total_n,
        coverage=coverage,
        confidence=confidence,
        nudge_search=truncated,
        use_custom_lines=use_custom_lines,
    )


def _cached_curator_lines(thread_id: str, fingerprint: str) -> list[str] | None:
    cached = _curation_cache.get(thread_id)
    if cached is None:
        return None
    cached_fp, lines = cached
    if cached_fp != fingerprint:
        return None
    return list(lines)


def _store_curator_cache(thread_id: str, fingerprint: str, lines: list[str]) -> None:
    _curation_cache[thread_id] = (fingerprint, list(lines))


async def _run_curator_cached(
    *,
    thread_id: str,
    fingerprint: str,
    ctx: TurnContext,
    provider: Provider,
    curator_provider: Provider | None,
    user_message: str,
) -> CuratedPromptParts:
    cached = _cached_curator_lines(thread_id, fingerprint)
    if cached is not None:
        _log.debug("[curation] cache hit thread_id=%s lines=%d", thread_id, len(cached))
        return CuratedPromptParts(cached, success=True)

    parts = await run_context_curator(
        ctx=ctx,
        provider=provider,
        curator_model=curator_model_id(ctx),
        user_message=user_message,
        curator_provider=curator_provider,
    )
    if parts.success:
        _store_curator_cache(thread_id, fingerprint, parts.memory_lines)
    return parts


async def prepare_memory_for_prompt(
    *,
    ctx: TurnContext,
    user_message: str,
    provider: Provider,
    curator_provider: Provider | None,
) -> MemoryPromptSelection:
    """Select memory lines for the system prompt (window / curator / hybrid)."""
    total = list(ctx.memory_index)
    if not curation_enabled_from_env() or not ctx.context_curation_enabled:
        return _selection_from_lines(total, total, use_custom_lines=False)

    if not curation_threshold_met(ctx):
        return _selection_from_lines(total, total, use_custom_lines=False)

    mode = curation_mode_from_env()
    window_n = memory_window_lines_from_env()
    window = memory_window_slice(total, window_n)
    fingerprint = memory_index_fingerprint(total)

    if mode == "window":
        return _selection_from_lines(window, total, use_custom_lines=len(window) < len(total))

    if mode == "curator":
        parts = await _run_curator_cached(
            thread_id=ctx.thread_id,
            fingerprint=fingerprint,
            ctx=ctx,
            provider=provider,
            curator_provider=curator_provider,
            user_message=user_message,
        )
        if parts.success:
            injected = list(parts.memory_lines)
            return _selection_from_lines(injected, total, use_custom_lines=True)
        return _selection_from_lines(total, total, use_custom_lines=False)

    # hybrid: recent window by default; curator when full index is token-heavy
    token_n = _env_int("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", 2000)
    token_heavy = memory_index_token_estimate(total) > token_n
    if not token_heavy:
        return _selection_from_lines(window, total, use_custom_lines=len(window) < len(total))

    parts = await _run_curator_cached(
        thread_id=ctx.thread_id,
        fingerprint=fingerprint,
        ctx=ctx,
        provider=provider,
        curator_provider=curator_provider,
        user_message=user_message,
    )
    if parts.success:
        injected = list(parts.memory_lines)
        return _selection_from_lines(injected, total, use_custom_lines=True)
    return _selection_from_lines(window, total, use_custom_lines=len(window) < len(total))
