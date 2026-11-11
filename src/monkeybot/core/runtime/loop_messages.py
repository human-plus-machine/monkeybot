"""Message / system-prompt shaping helpers for the agent turn loop."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence

from monkeybot.core.attachments.catalog import SessionAttachmentCatalog
from monkeybot.core.context import TurnContext
from monkeybot.core.context.epoch import ContextEpochTracker, EpochAdmit, fingerprint_text
from monkeybot.core.context.tool_result_ingress import summarize_tool_result_text
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.messages.transform_context import provider_visible_blocks, transform_context
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.prompts.headings import (
    RUNTIME_NOTES_HEADING,
    SYSTEM_CONTEXT_UPDATE_HEADING,
    heading_marker,
    is_system_context_update,
)
from monkeybot.core.prompts.prompt import (
    compose_stable_baseline_parts,
    compose_volatile_tail_parts,
)
from monkeybot.core.prompts.section_budget import SECTION_CHAR_CAPS, clip_head_and_tail
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    File,
    Image,
    Text,
    ToolRequest,
    ToolResponse,
)

from .context_budget import estimate_tokens_from_char_count
from .events import AgentEvent, ContextEpochStarted, PromptSection, SystemContextUpdated

logger = logging.getLogger("monkeybot.core.runtime.loop.loop_messages")


def _normalize_user_content(user_content: str | list[ContentBlock]) -> list[ContentBlock]:
    if isinstance(user_content, str):
        return [Text(text=user_content)]
    return list(user_content)


def _user_text_from_content(blocks: Sequence[ContentBlock]) -> str:
    return " ".join(b.text.strip() for b in blocks if isinstance(b, Text) and b.text.strip())


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


def _stable_text_and_live_parts(
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    *,
    attachment_catalog: SessionAttachmentCatalog | None = None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """System prefix, the sections that ride the tail, and the prefix split by name.

    The prefix is the cacheable baseline plus ``## Skills``. Skills are large and
    change rarely; a change still keeps the cache checkpoint that sits before
    that heading.     Date, memory, the todo list, background commands, and the current request
    change between user messages, so they stay out of the system text.
    """
    records = attachment_catalog.list_records() if attachment_catalog is not None else None
    stable_parts = compose_stable_baseline_parts(ctx, attachment_catalog=records)
    parts = compose_volatile_tail_parts(ctx, chat_messages=chat_messages)
    skills = parts.get("skills", "")
    if skills:
        stable_parts = {**stable_parts, "skills": skills}
    live = {name: text for name, text in parts.items() if name != "skills"}
    return "".join(stable_parts.values()), live, stable_parts


def _admit_system_context(
    epoch: ContextEpochTracker,
    ctx: TurnContext,
    chat_messages: Sequence[Message],
    *,
    attachment_catalog: SessionAttachmentCatalog | None = None,
) -> EpochAdmit:
    """Reconcile the system prefix and the live sections against the context epoch."""
    stable, live_parts, stable_parts = _stable_text_and_live_parts(
        ctx, chat_messages, attachment_catalog=attachment_catalog
    )
    live = "".join(live_parts.values())
    return epoch.reconcile(
        stable_baseline=stable,
        volatile_text=live,
        stable_fingerprint=fingerprint_text(stable),
        volatile_fingerprint=fingerprint_text(live),
        volatile_parts=live_parts,
        stable_parts=stable_parts,
    )


def _send_tail(context_tail: str, notes: str | None) -> str:
    """Context tail plus this call's one-shot notes, inside the same block.

    The notes used to be appended to the system text, which changed it for one
    call and changed it back on the next. Both changes sit before the messages,
    so the cache could not reuse anything after the stable system block. The
    tail is already outside that prefix: cache markers, the transcript, and
    :func:`_split_context_tail` treat a block starting with ``## System context
    update`` as send-time only. With no notes the tail is returned unchanged.
    """
    if not notes or not notes.strip():
        return context_tail
    raw = notes.strip()
    body = _clipped_runtime_notes(notes)
    cap = SECTION_CHAR_CAPS["runtime_notes"]
    if len(raw) > cap:
        logger.warning(
            "runtime notes truncated %s",
            kv(chars=len(raw), cap=cap),
        )
    head = context_tail.strip() or heading_marker(SYSTEM_CONTEXT_UPDATE_HEADING).strip()
    return f"{head}{RUNTIME_NOTES_HEADING}{body}\n"


def _append_notice_to_send_tail(messages: list[Message], notice: str) -> list[Message]:
    """Put a one-shot notice in the send-time tail of ``messages``.

    ``BEFORE_PROVIDER_REQUEST`` hooks run after the tail is attached. A notice
    spliced onto the system message would change that text for one call. The
    notice goes into the existing tail block, or into a new one when this call
    has no tail yet.
    """
    note = notice.strip()
    if not note:
        return messages
    addition = _send_tail("", note)
    if not messages:
        return [Message(role="user", content=[Text(text=addition)])]
    last = messages[-1]
    blocks = list(last.content)
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if isinstance(block, Text) and is_system_context_update(block.text):
            blocks[index] = Text(text=_send_tail(block.text, note))
            return [*messages[:-1], Message(role=last.role, content=blocks)]
    extra = Text(text=addition)
    if last.role == "user":
        return [*messages[:-1], Message(role="user", content=[*blocks, extra])]
    return [*messages, Message(role="user", content=[extra])]


def _messages_for_provider(
    system: Message,
    history: Sequence[Message],
    *,
    context_tail: str = "",
) -> list[Message]:
    """Leading system + history + the live tail, whenever that tail has text.

    The tail is user-role so all providers accept it (system is leading-only for
    Anthropic/Gemini/OpenAI adapters). When history already ends in a ``user``
    row (e.g. a tool-response turn), the tail is folded into that same message
    instead of appended as a new one — Anthropic and Gemini both reject (or, for
    Anthropic, only sometimes silently coalesce) two consecutive same-role messages.
    It is attached on every call, including the first of a user message, and it
    is not stored, so the system text and the history stay a stable prefix.
    """
    out: list[Message] = [system, *list(history)]
    update = context_tail.strip()
    if not update:
        return out
    update_block = Text(text=context_tail)
    if out[-1].role == "user":
        out[-1] = Message(role="user", content=[*out[-1].content, update_block])
    else:
        out.append(Message(role="user", content=[update_block]))
    return out


def _split_context_tail(messages: Sequence[Message]) -> tuple[list[Message], str]:
    """Messages without the live tail, and the tail text.

    The tail is attached at send time and is not part of stored history. The
    transcript records it beside the request, so a later call's copy of the
    same message stays byte-identical to what was written the first time.
    """
    if not messages:
        return [], ""
    last = messages[-1]
    kept: list[ContentBlock] = []
    tails: list[str] = []
    for block in last.content:
        if isinstance(block, Text) and is_system_context_update(block.text):
            tails.append(block.text)
        else:
            kept.append(block)
    if not tails:
        return list(messages), ""
    head = list(messages[:-1])
    if kept:
        head.append(Message(role=last.role, content=kept))
    return head, "\n\n".join(tails)


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
            text=admit.context_tail,
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


def _clipped_runtime_notes(extra: str | None) -> str:
    """Runtime-note body within its character cap, or ``""`` when there is nothing to add."""
    if not extra:
        return ""
    body = extra.strip()
    if not body:
        return ""
    cap = SECTION_CHAR_CAPS["runtime_notes"]
    if len(body) <= cap:
        return body
    return clip_head_and_tail(body, cap)


def _runtime_notes_piece(extra: str | None) -> str:
    """On-wire text appended for runtime notes, including the heading. Empty when unused."""
    body = _clipped_runtime_notes(extra)
    if not body:
        return ""
    return f"{RUNTIME_NOTES_HEADING}\n{body}\n"


def _append_extra_system_text(system: Message, extra: str | None) -> Message:
    """Return a new system Message with ``extra`` under a ``## Runtime notes`` section.

    Uses the same markdown heading style as the rest of the composed system prompt
    (``## Memory wake-up``, harness sections). When ``extra`` is empty/None the original
    message is returned unchanged.
    """
    if not extra:
        return system
    raw = extra.strip()
    piece = _runtime_notes_piece(extra)
    if not piece:
        return system
    cap = SECTION_CHAR_CAPS["runtime_notes"]
    if len(raw) > cap:
        logger.warning(
            "runtime notes truncated %s",
            kv(chars=len(raw), cap=cap),
        )
    base = "".join(b.text for b in system.content if isinstance(b, Text))
    return Message(role="system", content=[Text(text=f"{base}{piece}")])


def _combine_extras(*parts: str | None) -> str | None:
    """Join non-empty hook-injected fragments with blank lines; ``None`` if all empty."""
    kept = [p.strip() for p in parts if p and p.strip()]
    if not kept:
        return None
    return "\n\n".join(kept)


def _flatten_tool_result_for_summary(
    resp: ToolResponse, *, window_tokens: int | None = None
) -> str:
    parts: list[str] = []
    for b in resp.result:
        raw = b.text if isinstance(b, Text) else json.dumps(b.to_dict(), sort_keys=True)
        parts.append(summarize_tool_result_text(raw, window_tokens=window_tokens))
    return "".join(parts) or "(empty)"


def _summary_line_for_message(m: Message, *, window_tokens: int | None = None) -> str:
    """One summarizer-prompt line for ``m``; empty when nothing the model saw remains."""
    pieces: list[str] = []
    for b in provider_visible_blocks(m):
        if isinstance(b, Text):
            pieces.append(b.text)
        elif isinstance(b, ToolRequest):
            pieces.append(f"[tool_call: {b.name}({json.dumps(b.args, sort_keys=True)})]")
        elif isinstance(b, ToolResponse):
            body = _flatten_tool_result_for_summary(b, window_tokens=window_tokens)
            tag = "tool_error" if b.is_error else "tool_result"
            pieces.append(f"[{tag} {b.tool_name}: {body}]")
        else:
            pieces.append(f"[{type(b).__name__}]")
    if not pieces:
        return "" if m.content else f"{m.role}: (empty)"
    return f"{m.role}: {' '.join(pieces)}"


def _system_prompt_snapshot_text(system: Message, context_tail: str = "") -> str:
    """Plain string for :class:`SystemPromptSnapshot` (composed prompt + live tail)."""
    body = "".join(b.text for b in system.content if isinstance(b, Text))
    update = context_tail.strip()
    if not update:
        return body
    return f"{body}\n\n{update}"


def _prompt_section(name: str, chars: int) -> PromptSection:
    """One section row. ``chars`` is the on-wire contribution, heading included.

    ``cap_chars`` is the body cap, so a capped section can read a few characters
    over its cap because of the heading that introduces it.
    """
    return PromptSection(
        name=name,
        chars=chars,
        est_tokens=estimate_tokens_from_char_count(chars),
        cap_chars=SECTION_CHAR_CAPS.get(name),
    )


def build_prompt_sections(
    *,
    stable_parts: Mapping[str, str],
    volatile_parts: Mapping[str, str],
    runtime_notes: str | None,
    context_tail: str,
    tools_chars: int,
    snapshot_text: str,
) -> tuple[PromptSection, ...]:
    """Per-section size of the prompt that was just snapshotted.

    System sections sum to ``snapshot_text`` when the system message is the one
    the epoch composed. A hook that rewrote that message leaves ``unattributed``
    for the text the parts no longer explain. ``tools`` is the tool-schema JSON,
    which is not part of the snapshot text.
    """
    pieces: list[tuple[str, str]] = [
        *stable_parts.items(),
        *volatile_parts.items(),
        ("runtime_notes", _runtime_notes_piece(runtime_notes)),
    ]
    update = context_tail.strip()
    if update:
        pieces.append(("context_tail", f"\n\n{update}"))
    expected = "".join(text for _name, text in pieces)
    sections = [_prompt_section(name, len(text)) for name, text in pieces if text]
    if snapshot_text != expected:
        if snapshot_text.startswith(expected):
            gap = snapshot_text[len(expected) :]
        else:
            sections = []
            gap = snapshot_text
        if gap:
            sections.append(_prompt_section("unattributed", len(gap)))
    sections.append(_prompt_section("tools", tools_chars))
    return tuple(sections)


def _snapshot_system_message(provider_messages: Sequence[Message], fallback: Message) -> Message:
    """Prefer the post-hook leading system message actually sent to the provider."""
    if provider_messages and provider_messages[0].role == "system":
        return provider_messages[0]
    return fallback


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
