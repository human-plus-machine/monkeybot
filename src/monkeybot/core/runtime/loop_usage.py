"""Usage accounting and provider prompt-token helpers for the agent turn loop."""

from __future__ import annotations

from collections.abc import Sequence

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.config.snapshot import RuntimeConfig, env_value_or_current
from monkeybot.core.context import TurnContext
from monkeybot.core.context.epoch import ContextEpochTracker, fingerprint_text
from monkeybot.core.llm.provider import (
    Message,
    Provider,
    ProviderCallHints,
    UsageEvent,
    cache_retention_from_env,
    provider_count_input_tokens,
)
from monkeybot.core.llm.usage import Usage
from monkeybot.core.messages import convert_to_provider
from monkeybot.core.types.types_tools import ToolDef

from .events import UsageTotals
from .loop_messages import (
    _is_routine_resume_turn,
    _messages_for_provider,
    _send_tail,
    _stable_text_and_live_parts,
    _system_message_from_text,
)


def _effective_max_turns(max_turns: int | None, cfg: RuntimeConfig | None = None) -> int:
    if max_turns is not None:
        return max_turns
    raw = env_value_or_current(cfg, "MAX_TURNS", "1000")
    return int(raw)


def _usage_to_totals(u: Usage) -> UsageTotals:
    return UsageTotals(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cached_tokens=u.cached_tokens,
        cache_read_tokens=u.cache_read_tokens,
        cache_creation_tokens=u.cache_creation_tokens,
        cost_usd=u.cost_usd,
        duration_ms=u.duration_ms,
        estimated_prompt_tokens=u.estimated_prompt_tokens,
    )


def _merge_usage_event(usage: Usage, ev: UsageEvent) -> None:
    usage.input_tokens += ev.input_tokens
    usage.output_tokens += ev.output_tokens
    usage.cached_tokens += ev.cached_tokens
    usage.cache_read_tokens += ev.cache_read_tokens
    usage.cache_creation_tokens += ev.cache_creation_tokens


def reported_prompt_tokens(
    provider_name: str,
    *,
    input_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> int:
    """Total prompt size from one usage report.

    Gemini's prompt count already includes cached content. Anthropic, Bedrock,
    and the OpenAI-compatible adapters report uncached input separately from
    cache read and cache creation.
    """
    if provider_name == "gemini":
        return input_tokens
    return input_tokens + cache_read_tokens + cache_creation_tokens


_THINKING_BUDGET_PROVIDERS = frozenset(
    {"gemini", "claude", "ollama", "ollama-cloud", "ollama-local"}
)


def _stream_thinking_budget(
    provider: Provider,
    resolved_messages: Sequence[Message],
    cfg: RuntimeConfig | None = None,
) -> int | None:
    """Per-call thinking budget override; None keeps the provider default."""
    if provider.name not in _THINKING_BUDGET_PROVIDERS:
        return None
    raw = env_value_or_current(cfg, "MONKEYBOT_RESUME_THINKING_BUDGET", "").strip()
    if not raw:
        return None
    try:
        resume_budget = int(raw)
    except ValueError:
        return None
    if _is_routine_resume_turn(resolved_messages):
        return resume_budget
    return None


async def _provider_prompt_input_tokens(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
    *,
    model: str,
    thinking_budget: int | None = None,
    vertex_google_search: bool = False,
    hints: ProviderCallHints | None = None,
) -> int:
    return await provider_count_input_tokens(
        provider,
        messages,
        tools,
        model=model,
        thinking_budget=thinking_budget,
        vertex_google_search=vertex_google_search,
        hints=hints,
    )


def _provider_call_hints(ctx: TurnContext) -> ProviderCallHints:
    return ProviderCallHints(
        session_id=ctx.session_id,
        cache_retention=cache_retention_from_env(ctx.config),
        workspace_root=ctx.workspace_root,
        context_window_tokens=ctx.context_window_tokens,
    )


async def _prompt_input_tokens_for_history(
    *,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    provider: Provider,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    extra_system_text: str | None = None,
    vertex_google_search: bool = False,
    epoch: ContextEpochTracker | None = None,
) -> int:
    """Provider-accurate prompt size for history rows already persisted (e.g. post-assistant).

    The count is the wire shape: the stable prefix (baseline plus skills) plus
    the live tail. When ``epoch`` is supplied that shape comes from a
    non-mutating :meth:`ContextEpochTracker.peek`; otherwise a fresh tracker is
    peeked, which is the shape of the first call of a user message. Notes in
    ``extra_system_text`` are counted in the tail, where the request sends them.
    The live tracker is never mutated by a budget recount.
    """
    stable, live_parts, stable_parts = _stable_text_and_live_parts(
        ctx, chat_messages, attachment_catalog=attachment_catalog
    )
    live = "".join(live_parts.values())
    admit = (epoch or ContextEpochTracker()).peek(
        stable_baseline=stable,
        volatile_text=live,
        stable_fingerprint=fingerprint_text(stable),
        volatile_fingerprint=fingerprint_text(live),
        volatile_parts=live_parts,
        stable_parts=stable_parts,
    )
    system = _system_message_from_text(admit.leading_system_text)
    context_tail = _send_tail(admit.context_tail, extra_system_text)
    resolved_messages = convert_to_provider(
        chat_messages,
        attachment_store=attachment_store,
        session_id=ctx.session_id,
    )
    provider_messages = _messages_for_provider(system, resolved_messages, context_tail=context_tail)
    return await _provider_prompt_input_tokens(
        provider,
        provider_messages,
        ctx.tools,
        model=ctx.model,
        vertex_google_search=vertex_google_search,
        hints=_provider_call_hints(ctx),
    )
