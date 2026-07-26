"""Usage accounting and provider prompt-token helpers for the agent turn loop."""

from __future__ import annotations

import os
from collections.abc import Sequence

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.context import TurnContext
from monkeybot.core.context.epoch import ContextEpochTracker, fingerprint_text
from monkeybot.core.context.memory_prompt import MemoryPromptSelection
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
from monkeybot.core.prompts.prompt import (
    compose_stable_baseline,
    compose_volatile_tail_parts,
)
from monkeybot.core.types.types_tools import ToolDef

from .events import UsageTotals
from .loop_messages import (
    _append_extra_system_text,
    _is_routine_resume_turn,
    _messages_for_provider,
    _system_message_from_text,
)


def _effective_max_turns(max_turns: int | None) -> int:
    if max_turns is not None:
        return max_turns
    return int(os.getenv("MAX_TURNS", "1000"))


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


def _stream_thinking_budget(
    provider: Provider,
    resolved_messages: Sequence[Message],
) -> int | None:
    """Per-call thinking budget override; None keeps the provider default."""
    if provider.name not in ("gemini", "claude", "ollama"):
        return None
    raw = os.environ.get("MONKEYBOT_RESUME_THINKING_BUDGET", "").strip()
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
        session_id=ctx.thread_id,
        cache_retention=cache_retention_from_env(),
    )


async def _prompt_input_tokens_for_history(
    *,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    provider: Provider,
    attachment_store: AttachmentStore | None,
    attachment_catalog: SessionAttachmentCatalog | None,
    memory_selection: MemoryPromptSelection | None = None,
    extra_system_text: str | None = None,
    vertex_google_search: bool = False,
    epoch: ContextEpochTracker | None = None,
) -> int:
    """Provider-accurate prompt size for history rows already persisted (e.g. post-assistant).

    When ``epoch`` is supplied, the count reflects the true wire shape for the
    *current* epoch state — leading (cached) baseline plus any pending
    mid-conversation update — via a non-mutating :meth:`ContextEpochTracker.peek`.
    Falls back to a flat stable+volatile concatenation when no tracker is given
    (e.g. summarizer/curator calls that have no epoch of their own). Either way,
    the live tracker is never mutated by a budget recount.
    """
    catalog = (
        attachment_catalog.list_records() if attachment_catalog is not None else None
    )
    stable = compose_stable_baseline(ctx, attachment_catalog=catalog)
    volatile_parts = compose_volatile_tail_parts(
        ctx, chat_messages=chat_messages, memory_selection=memory_selection
    )
    volatile = "".join(volatile_parts.values())
    mid_conversation_update = ""
    if epoch is not None:
        volatile_content = "".join(
            text for name, text in volatile_parts.items() if name != "current_date"
        )
        admit = epoch.peek(
            stable_baseline=stable,
            volatile_text=volatile,
            stable_fingerprint=fingerprint_text(stable),
            volatile_fingerprint=fingerprint_text(volatile),
            volatile_content_text=volatile_content,
        )
        body = admit.leading_system_text
        mid_conversation_update = admit.mid_conversation_update
    else:
        body = stable + volatile
    system = _append_extra_system_text(_system_message_from_text(body), extra_system_text)
    resolved_messages = convert_to_provider(
        chat_messages,
        attachment_store=attachment_store,
        session_id=ctx.thread_id,
    )
    provider_messages = _messages_for_provider(
        system, resolved_messages, mid_conversation_update=mid_conversation_update
    )
    return await _provider_prompt_input_tokens(
        provider, provider_messages, ctx.tools, model=ctx.model,
        vertex_google_search=vertex_google_search,
        hints=_provider_call_hints(ctx),
    )
