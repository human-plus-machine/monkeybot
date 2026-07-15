"""Message / system-prompt shaping helpers for the agent turn loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.context import TurnContext
from monkeybot.core.context.epoch import ContextEpochTracker, EpochAdmit, fingerprint_text
from monkeybot.core.context.memory_prompt import MemoryPromptSelection
from monkeybot.core.context.tool_result_ingress import summarize_tool_result_text
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages import transform_context
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.prompts.prompt import (
    RUNTIME_NOTES_HEADING,
    compose_stable_baseline,
    compose_volatile_tail_parts,
)
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    File,
    Image,
    Text,
    ToolRequest,
    ToolResponse,
)

from .events import AgentEvent, ContextEpochStarted, SystemContextUpdated

logger = logging.getLogger("monkeybot.core.runtime.loop_messages")


def _normalize_user_content(user_content: str | list[ContentBlock]) -> list[ContentBlock]:
    if isinstance(user_content, str):
        return [Text(text=user_content)]
    return list(user_content)


def _user_text_from_content(blocks: Sequence[ContentBlock]) -> str:
    return " ".join(
        b.text.strip() for b in blocks if isinstance(b, Text) and b.text.strip()
    )


def _blocks_to_sse_summary(blocks: Sequence[ContentBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        elif isinstance(block, Image):
            meta = block.metadata or {}
            att_id = meta.get("attachment_id", "")
            parts.append(f"[loaded image {att_id}]" if att_id else "[loaded image]")
        elif isinstance(block, File):
            parts.append("[loaded pdf]")
    return "\n".join(parts)


def _system_message_from_text(body: str) -> Message:
    return Message(role="system", content=[Text(text=body)])


def _admit_system_context(
    epoch: ContextEpochTracker,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    *,
    memory_selection: MemoryPromptSelection | None = None,
    attachment_catalog: SessionAttachmentCatalog | None = None,
) -> EpochAdmit:
    """Compose stable/volatile tails and reconcile against the current context epoch."""
    catalog = (
        attachment_catalog.list_records() if attachment_catalog is not None else None
    )
    stable = compose_stable_baseline(ctx, attachment_catalog=catalog)
    volatile_parts = compose_volatile_tail_parts(
        ctx, chat_messages=chat_messages, memory_selection=memory_selection
    )
    volatile = "".join(volatile_parts.values())
    # Excludes "current_date": it is never empty (always today's date), so it
    # must not count as "content" when deciding whether volatile sections were
    # cleared for the "## System context update" mid-epoch message.
    volatile_content = "".join(
        text for name, text in volatile_parts.items() if name != "current_date"
    )
    return epoch.reconcile(
        stable_baseline=stable,
        volatile_text=volatile,
        stable_fingerprint=fingerprint_text(stable),
        volatile_fingerprint=fingerprint_text(volatile),
        volatile_part_fingerprints={
            name: fingerprint_text(text) for name, text in volatile_parts.items()
        },
        volatile_content_text=volatile_content,
    )


def _messages_for_provider(
    system: Message,
    history: Sequence[Message],
    *,
    mid_conversation_update: str = "",
) -> list[Message]:
    """Leading system + history + optional chronological system-context update.

    The update is user-role so all providers accept mid-conversation updates
    (system is leading-only for Anthropic/Gemini/OpenAI adapters). When history
    already ends in a ``user`` row (e.g. a tool-response turn), the update is
    folded into that same message instead of appended as a new one — Anthropic
    and Gemini both reject (or, for Anthropic, only sometimes silently coalesce)
    two consecutive same-role messages.
    """
    out: list[Message] = [system, *list(history)]
    update = mid_conversation_update.strip()
    if not update:
        return out
    update_block = Text(text=mid_conversation_update)
    if out[-1].role == "user":
        out[-1] = Message(role="user", content=[*out[-1].content, update_block])
    else:
        out.append(Message(role="user", content=[update_block]))
    return out


async def _load_agent_chat_history(history: HistoryStore, thread_id: str) -> list[Message]:
    """Load transcript rows and apply agent-facing transforms (integrity + strip UI)."""
    return transform_context(await history.load(thread_id))


def _epoch_events(
    admit: EpochAdmit,
    *,
    request_id: str,
    thread_id: str,
) -> list[AgentEvent]:
    if admit.kind == "unchanged":
        return []
    logger.debug(
        "context epoch %s",
        kv(
            request_id=request_id,
            thread_id=thread_id,
            kind=admit.kind,
            epoch_id=admit.epoch_id,
            changed_sources=",".join(admit.changed_sources),
        ),
    )
    if admit.kind == "new_epoch":
        return [
            ContextEpochStarted(
                request_id=request_id,
                epoch_id=admit.epoch_id,
                changed_sources=list(admit.changed_sources),
            )
        ]
    return [
        SystemContextUpdated(
            request_id=request_id,
            epoch_id=admit.epoch_id,
            changed_sources=list(admit.changed_sources),
        )
    ]


def _provider_messages_prompt_summary(messages: Sequence[Message]) -> str:
    """Compact prompt text for observability (Langfuse ``gen_ai.prompt`` / observation input)."""
    lines: list[str] = []
    for msg in messages:
        parts: list[str] = []
        for block in msg.content:
            if isinstance(block, Text):
                parts.append(block.text)
            elif isinstance(block, ToolRequest):
                parts.append(f"[tool_call {block.name}]")
            elif isinstance(block, ToolResponse):
                parts.append(f"[tool_result {block.tool_name}]")
        text = " ".join(parts).strip()
        if text:
            lines.append(f"{msg.role}: {text}")
    return "\n".join(lines)


def _append_extra_system_text(system: Message, extra: str | None) -> Message:
    """Return a new system Message with ``extra`` under a ``## Runtime notes`` section.

    Uses the same markdown heading style as the rest of the composed system prompt
    (``## Memory index``, harness sections). When ``extra`` is empty/None the original
    message is returned unchanged.
    """
    if not extra:
        return system
    base = "".join(b.text for b in system.content if isinstance(b, Text))
    wrapped = f"{base}{RUNTIME_NOTES_HEADING}\n{extra.strip()}\n"
    return Message(role="system", content=[Text(text=wrapped)])


def _combine_extras(*parts: str | None) -> str | None:
    """Join non-empty hook-injected fragments with blank lines; ``None`` if all empty."""
    kept = [p.strip() for p in parts if p and p.strip()]
    if not kept:
        return None
    return "\n\n".join(kept)


def _flatten_tool_result_for_summary(resp: ToolResponse) -> str:
    parts: list[str] = []
    for b in resp.result:
        if isinstance(b, Text):
            parts.append(summarize_tool_result_text(b.text))
        else:
            parts.append(summarize_tool_result_text(json.dumps(b.to_dict(), sort_keys=True)))
    return "".join(parts) or "(empty)"


def _summary_line_for_message(m: Message) -> str:
    pieces: list[str] = []
    for b in m.content:
        if isinstance(b, Text):
            pieces.append(b.text)
        elif isinstance(b, ToolRequest):
            pieces.append(f"[tool_call: {b.name}({json.dumps(b.args, sort_keys=True)})]")
        elif isinstance(b, ToolResponse):
            body = _flatten_tool_result_for_summary(b)
            tag = "tool_error" if b.is_error else "tool_result"
            pieces.append(f"[{tag} {b.tool_name}: {body}]")
        else:
            pieces.append(f"[{type(b).__name__}]")
    joined = " ".join(pieces) if pieces else "(empty)"
    return f"{m.role}: {joined}"


def _system_prompt_snapshot_text(
    system: Message, mid_conversation_update: str = ""
) -> str:
    """Plain string for :class:`SystemPromptSnapshot` (composed prompt + mid-epoch update)."""
    body = "".join(b.text for b in system.content if isinstance(b, Text))
    update = mid_conversation_update.strip()
    if not update:
        return body
    return f"{body}\n\n{update}"


def _is_resume_turn(resolved_messages: Sequence[Message]) -> bool:
    """True when the model continues after tool results, not a new user question."""
    if not resolved_messages:
        return False
    last = resolved_messages[-1]
    if last.role != "user":
        return False
    has_tool_response = any(isinstance(b, ToolResponse) for b in last.content)
    has_user_text = any(isinstance(b, Text) and b.text.strip() for b in last.content)
    return has_tool_response and not has_user_text


def _is_routine_resume_turn(resolved_messages: Sequence[Message]) -> bool:
    """Resume turn with only successful tool results (safe to reduce thinking budget)."""
    if not _is_resume_turn(resolved_messages):
        return False
    last = resolved_messages[-1]
    tool_responses = [b for b in last.content if isinstance(b, ToolResponse)]
    return bool(tool_responses) and all(not b.is_error for b in tool_responses)
