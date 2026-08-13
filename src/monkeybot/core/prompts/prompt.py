"""Compose the full system prompt: user AGENT.md plus runtime injections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from monkeybot.core.attachments.catalog import AttachmentRecord
from monkeybot.core.context import TurnContext
from monkeybot.core.context.memory_prompt import MemoryPromptSelection
from monkeybot.core.knowledge.config import knowledge_enabled_from_config
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.harness_prompt import (
    emission_style_terse_from_env,
    harness_fixed_context,
)
from monkeybot.core.prompts.headings import (
    CURRENT_DATE_HEADING,
    CURRENT_REQUEST_HEADING,
    MEMORY_INDEX_HEADING,
    SKILLS_HEADING,
    TODO_LIST_HEADING,
)
from monkeybot.core.tools.sandbox_executor import SandboxConfig
from monkeybot.core.types.content_blocks import Text

# Cap injected user text so long pastes do not dominate the context window.
_MAX_CURRENT_REQUEST_CHARS = 8000


def _user_text_flat(m: Message) -> str:
    """Concatenate Text blocks from a user turn (tool results use other block types)."""
    return "".join(b.text for b in m.content if isinstance(b, Text))


def _last_user_content(messages: Sequence[Message]) -> tuple[str, int] | None:
    """Return (flattened text, index) of the last user message that carries text.

    User rows that contain only tool-result blocks (no Text) are skipped so that
    the originating user request — not the synthetic tool-response turn — is
    used as the "Current request" anchor.
    """
    last: tuple[str, int] | None = None
    for i, m in enumerate(messages):
        if m.role == "user":
            text = _user_text_flat(m)
            if text:
                last = (text, i)
    return last


def latest_user_message_text(messages: Sequence[Message] | None) -> str | None:
    """Return stripped text of the last user message, or None."""
    if not messages:
        return None
    last = _last_user_content(messages)
    if last is None:
        return None
    t = last[0].strip()
    return t or None


def _current_request_block(chat_messages: Sequence[Message] | None) -> str:
    """When the transcript continues after the user's last message, restate it in-system.

    Avoids duplicating the latest user row when it is already the final message in
    ``chat_messages`` (that row is still sent as a normal user message).
    """
    if not chat_messages:
        return ""
    last_user = _last_user_content(chat_messages)
    if last_user is None:
        return ""
    text, _idx = last_user
    if not text.strip():
        return ""
    last = chat_messages[-1]
    if last.role == "user" and _user_text_flat(last).strip():
        return ""
    clipped = text.strip()
    if len(clipped) > _MAX_CURRENT_REQUEST_CHARS:
        clipped = clipped[:_MAX_CURRENT_REQUEST_CHARS].rstrip() + "\n…(truncated)"
    return (
        f"{CURRENT_REQUEST_HEADING}"
        "The conversation has continued with assistant or tool messages since this "
        "user message; treat it as the active task.\n\n"
        f"{clipped}"
    )


def _session_attachments_block(catalog: Sequence[AttachmentRecord] | None) -> str:
    if not catalog:
        return ""
    lines = [
        f"- {r.attachment_id} ({r.filename}, {r.mime_type}): {r.description}"
        for r in catalog
    ]
    return "\n\n## Session attachments\n" + "\n".join(lines)


def _current_date_block() -> str:
    """Host-local calendar date as machine-stable ``YYYY-MM-DD`` (volatile).

    Lives in the volatile tail so a day rollover does not bust the stable
    cache prefix; within a calendar day the fingerprint is stable.
    """
    return f"{CURRENT_DATE_HEADING}{date.today().isoformat()}"


def _memory_block(
    ctx: TurnContext,
    memory_selection: MemoryPromptSelection | None,
) -> str:
    if memory_selection is not None:
        mem_lines = list(memory_selection.lines)
    else:
        mem_lines = list(ctx.memory_index)

    memory_text = "\n".join(mem_lines) if mem_lines else ""
    mem_block = f"{MEMORY_INDEX_HEADING}{memory_text}" if memory_text else ""
    return mem_block


def _skills_section(ctx: TurnContext) -> str:
    skill_lines = [f"- {s.name}" for s in ctx.skills]
    skills_block = "\n".join(skill_lines)
    return f"{SKILLS_HEADING}{skills_block}" if skills_block else ""


def _todo_list_section(ctx: TurnContext) -> str:
    store = ctx.todo_store
    if store is None:
        return ""
    lines = store.format_lines()
    return f"{TODO_LIST_HEADING}{lines}" if lines else ""


def _harness_text(ctx: TurnContext) -> str:
    include_task = any(t.name == "task" for t in ctx.tools)
    include_web_search = any(t.name == "web_search" for t in ctx.tools)
    include_todo_list = any(t.name == "todo_list" for t in ctx.tools)
    return harness_fixed_context(
        include_task_tool=include_task,
        include_web_search=include_web_search,
        include_todo_list=include_todo_list,
        include_knowledge_search=knowledge_enabled_from_config(),
        include_memory_teaching=ctx.memory is not None,
        workspace_root=str(ctx.workspace_root) if ctx.workspace_root is not None else "(not set)",
        memory_storage_uri=ctx.memory.uri if ctx.memory is not None else "(not set)",
        run_command_opensandbox=SandboxConfig.from_env().enabled,
        subagent_personas=ctx.subagent_personas,
        emission_style=emission_style_terse_from_env(),
        catalog_mcp_servers=ctx.catalog_mcp_servers,
        scheduled_loops_available=ctx.scheduled_loops_available,
    )


def compose_stable_baseline(
    ctx: TurnContext,
    *,
    attachment_catalog: Sequence[AttachmentRecord] | None = None,
) -> str:
    """Cacheable prefix: AGENT.md + harness + session attachments."""
    harness = _harness_text(ctx)
    attachments = _session_attachments_block(attachment_catalog)
    return f"{ctx.agent_md}\n\n{harness}{attachments}"


def compose_volatile_tail(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
    memory_selection: MemoryPromptSelection | None = None,
) -> str:
    """Volatile tail: current date + memory index + skills + current-request anchor."""
    return "".join(
        compose_volatile_tail_parts(
            ctx, chat_messages=chat_messages, memory_selection=memory_selection
        ).values()
    )


def compose_volatile_tail_parts(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
    memory_selection: MemoryPromptSelection | None = None,
) -> dict[str, str]:
    """Same sections as :func:`compose_volatile_tail`, individually named.

    Lets callers (e.g. ``ContextEpochTracker``) attribute a mid-epoch volatile
    change to the specific source that moved — current date, memory, skills,
    todo list, or the current-request anchor — instead of a catch-all "volatile" label.
    """
    return {
        "current_date": _current_date_block(),
        "memory": _memory_block(ctx, memory_selection),
        "skills": _skills_section(ctx),
        "todos": _todo_list_section(ctx),
        "current_request": _current_request_block(chat_messages),
    }


def compose_system_prompt(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
    memory_selection: MemoryPromptSelection | None = None,
    attachment_catalog: Sequence[AttachmentRecord] | None = None,
) -> str:
    """Build the system string: AGENT.md, harness, attachments, then volatile tail.

    ``ctx.agent_md`` is the operator-authored base prompt (typically from AGENT.md).
    Stable sections (harness, attachments) precede volatile curation (current date,
    memory, skills, todo list, current-request anchor) so implicit and explicit prompt caching
    can hit a contiguous prefix across turns.

    When ``memory_selection`` is set, its lines are used
    instead of the full ``ctx.memory_index``. Skill names are always taken from
    ``ctx.skills`` (zero-cost discovery); use ``list_skills``/``read_file`` for the
    skills root path and full ``SKILL.md`` procedure.
    """
    stable = compose_stable_baseline(ctx, attachment_catalog=attachment_catalog)
    volatile = compose_volatile_tail(
        ctx, chat_messages=chat_messages, memory_selection=memory_selection
    )
    return f"{stable}{volatile}"
