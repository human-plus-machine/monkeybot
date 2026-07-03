"""Compose the full system prompt: user AGENT.md plus runtime injections."""

from __future__ import annotations

from collections.abc import Sequence

from monkeybot.core.attachments.catalog import AttachmentRecord
from monkeybot.core.context import TurnContext
from monkeybot.core.context.memory_prompt import MemoryPromptSelection
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.harness_prompt import (
    emission_style_terse_from_env,
    harness_fixed_context,
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
        "\n\n## Current request\n"
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


def compose_system_prompt(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
    memory_selection: MemoryPromptSelection | None = None,
    attachment_catalog: Sequence[AttachmentRecord] | None = None,
) -> str:
    """Build the system string: AGENT.md, harness, attachments, then volatile tail.

    ``ctx.agent_md`` is the operator-authored base prompt (typically from AGENT.md).
    Stable sections (harness, attachments) precede volatile curation (memory, skills,
    current-request anchor) so implicit and explicit prompt caching can hit a contiguous
    prefix across turns.

    When ``memory_selection`` is set, its lines (and optional search nudge) are used
    instead of the full ``ctx.memory_index``. Skill names are always taken from
    ``ctx.skills`` (zero-cost discovery); use ``list_skills``/``read_file`` for the
    skills root path and full ``SKILL.md`` procedure.
    """
    task = _current_request_block(chat_messages)

    if memory_selection is not None:
        mem_lines = list(memory_selection.lines)
    else:
        mem_lines = list(ctx.memory_index)

    memory_bullets = "\n".join(f"- {line}" for line in mem_lines) if mem_lines else ""
    mem_block = f"\n\n## Memory index\n{memory_bullets}" if memory_bullets else ""
    if memory_selection is not None and memory_selection.nudge_search:
        shown = len(memory_selection.lines)
        total = memory_selection.total_lines
        mem_block += (
            f"\n\n## Memory\n"
            f"Showing {shown} of {total} index entries "
            f"(coverage {memory_selection.coverage:.0%}, confidence {memory_selection.confidence:.0%}). "
            "Use `search_memory` with keywords when the task may depend on older or unstated context."
        )

    skill_lines = [f"- {s.name}" for s in ctx.skills]
    skills_block = "\n".join(skill_lines)
    skills_section = f"\n\n## Skills\n{skills_block}" if skills_block else ""

    include_task = any(t.name == "task" for t in ctx.tools)
    include_web_search = any(t.name == "web_search" for t in ctx.tools)
    harness = harness_fixed_context(
        include_task_tool=include_task,
        include_web_search=include_web_search,
        workspace_root=str(ctx.workspace_root) if ctx.workspace_root is not None else "(not set)",
        memory_storage_uri=ctx.memory.uri if ctx.memory is not None else "(not set)",
        run_command_opensandbox=SandboxConfig.from_env().enabled,
        subagent_personas=ctx.subagent_personas,
        emission_style=emission_style_terse_from_env(),
    )

    attachments = _session_attachments_block(attachment_catalog)

    stable = f"{ctx.agent_md}\n\n{harness}{attachments}"
    volatile = f"{mem_block}{skills_section}{task}"
    return f"{stable}{volatile}"
