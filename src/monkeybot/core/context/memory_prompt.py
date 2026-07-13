"""Memory index selection for the system prompt: recent window, curator when token-heavy."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from monkeybot.core.context import TurnContext
from monkeybot.core.context.curator import (
    CuratedPromptParts,
    _env_int,
    curation_enabled_from_env,
    curator_model_id,
    memory_index_token_estimate,
    run_context_curator,
)
from monkeybot.core.llm.provider import Provider
from monkeybot.core.logging_utils import kv
from monkeybot.core.memory.index_format import memory_window_slice

_log = logging.getLogger(__name__)

# Process-local: thread_id -> (cache_key, curated_lines). Evicted on session teardown.
# cache_key covers index content + user message so query-aware picks are not reused.
_curation_cache: dict[str, tuple[str, list[str]]] = {}


def reset_curation_cache_for_tests() -> None:
    _curation_cache.clear()


def evict_curation_cache(thread_id: str) -> None:
    """Drop the cached curator selection for a thread (call on session removal)."""
    _curation_cache.pop(thread_id, None)


@dataclass(frozen=True)
class MemoryPromptSelection:
    """Memory lines and structural coverage for volatile system-prompt injection."""

    lines: list[str]
    total_lines: int
    coverage: float
    confidence: float
    nudge_search: bool
    use_custom_lines: bool


def memory_window_lines_from_env() -> int:
    return max(1, _env_int("CONTEXT_CURATION_MEMORY_WINDOW_LINES", 12))


def memory_token_threshold_from_env() -> int:
    return max(1, _env_int("CONTEXT_CURATION_MEMORY_TOKEN_THRESHOLD", 2000))


def memory_index_fingerprint(lines: list[str]) -> str:
    """Stable hash of index lines; used to detect mid-turn INDEX refreshes."""
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _curator_cache_key(lines: list[str], user_message: str) -> str:
    """Fingerprint for curator cache: index + query (curator is query-aware)."""
    payload = "\n".join(lines) + "\0" + user_message
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


def _cached_curator_lines(thread_id: str, cache_key: str) -> list[str] | None:
    cached = _curation_cache.get(thread_id)
    if cached is None:
        return None
    cached_key, lines = cached
    if cached_key != cache_key:
        return None
    return list(lines)


def _store_curator_cache(thread_id: str, cache_key: str, lines: list[str]) -> None:
    _curation_cache[thread_id] = (cache_key, list(lines))


async def _run_curator_cached(
    *,
    thread_id: str,
    cache_key: str,
    ctx: TurnContext,
    provider: Provider,
    curator_provider: Provider | None,
    user_message: str,
    max_memory_lines: int,
) -> CuratedPromptParts:
    cached = _cached_curator_lines(thread_id, cache_key)
    if cached is not None:
        _log.debug(
            "curation cache hit %s",
            kv(thread_id=thread_id, lines=len(cached)),
        )
        return CuratedPromptParts(cached, success=True)

    _log.debug(
        "curation cache miss %s",
        kv(thread_id=thread_id, index_lines=len(ctx.memory_index)),
    )
    parts = await run_context_curator(
        ctx=ctx,
        provider=provider,
        curator_model=curator_model_id(ctx),
        user_message=user_message,
        max_memory_lines=max_memory_lines,
        curator_provider=curator_provider,
    )
    if parts.success:
        _store_curator_cache(thread_id, cache_key, parts.memory_lines)
    return parts


async def prepare_memory_for_prompt(
    *,
    ctx: TurnContext,
    user_message: str,
    provider: Provider,
    curator_provider: Provider | None,
) -> MemoryPromptSelection:
    """Select memory lines for the system prompt.

    Default path: recent window. When the full index is token-heavy, optionally
    call the LLM curator; on curator failure, fall back to the window.
    """
    total = list(ctx.memory_index)
    if not curation_enabled_from_env() or not ctx.context_curation_enabled:
        return _selection_from_lines(total, total, use_custom_lines=False)

    window_n = memory_window_lines_from_env()
    token_n = memory_token_threshold_from_env()
    tokens = memory_index_token_estimate(total)
    exceeds_window = len(total) > window_n
    token_heavy = tokens > token_n

    if not exceeds_window and not token_heavy:
        return _selection_from_lines(total, total, use_custom_lines=False)

    window = memory_window_slice(total, window_n)
    if not token_heavy:
        _log.debug(
            "curation window %s",
            kv(
                thread_id=ctx.thread_id,
                injected=len(window),
                total=len(total),
                tokens=tokens,
            ),
        )
        return _selection_from_lines(window, total, use_custom_lines=len(window) < len(total))

    parts = await _run_curator_cached(
        thread_id=ctx.thread_id,
        cache_key=_curator_cache_key(total, user_message),
        ctx=ctx,
        provider=provider,
        curator_provider=curator_provider,
        user_message=user_message,
        max_memory_lines=window_n,
    )
    if parts.success:
        injected = list(parts.memory_lines)
        _log.info(
            "curation curator %s",
            kv(
                thread_id=ctx.thread_id,
                injected=len(injected),
                total=len(total),
                tokens=tokens,
            ),
        )
        return _selection_from_lines(injected, total, use_custom_lines=True)

    _log.warning(
        "curation curator failed; using window %s",
        kv(
            thread_id=ctx.thread_id,
            window=len(window),
            total=len(total),
            tokens=tokens,
        ),
    )
    return _selection_from_lines(window, total, use_custom_lines=len(window) < len(total))
