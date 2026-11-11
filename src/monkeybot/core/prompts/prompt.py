"""Compose the full system prompt: user AGENT.md plus runtime injections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from monkeybot.core.attachments.catalog import AttachmentRecord
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.harness_prompt import (
    emission_style_terse_from_env,
    harness_fixed_context,
)
from monkeybot.core.prompts.headings import (
    BACKGROUND_TASKS_HEADING,
    CURRENT_DATE_HEADING,
    CURRENT_REQUEST_HEADING,
    INVOKED_SKILL_HEADING,
    MEMORY_INDEX_HEADING,
    PROJECT_INSTRUCTIONS_HEADING,
    REPO_OVERVIEW_HEADING,
    SKILLS_HEADING,
    TODO_LIST_HEADING,
)
from monkeybot.core.prompts.section_budget import SECTION_CHAR_CAPS, clip_lines
from monkeybot.core.tools.sandbox_executor import SandboxConfig
from monkeybot.core.types.content_blocks import Text
from monkeybot.todo_list.store import TodoItem

# Cap injected user text so long pastes do not dominate the context window.
_MAX_CURRENT_REQUEST_CHARS = 8000
_TODO_CLIP_NOTE = "Long items are clipped. Start an item to see its full text."
_ELLIPSIS = "…"
# One-line skill summaries; the full procedure stays in SKILL.md.
_MAX_SKILL_DESCRIPTION_CHARS = 200
# Total budget for skill descriptions; skills past it are listed by name only.
_MAX_SKILL_DESCRIPTIONS_TOTAL_CHARS = 4000


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
    lines = [f"- {r.attachment_id} ({r.filename}, {r.mime_type}): {r.description}" for r in catalog]
    return "\n\n## Session attachments\n" + "\n".join(lines)


def _current_date_block() -> str:
    """Host-local calendar date as machine-stable ``YYYY-MM-DD`` (volatile).

    Lives in the volatile tail so a day rollover does not bust the stable
    cache prefix; within a calendar day the fingerprint is stable.
    """
    return f"{CURRENT_DATE_HEADING}{date.today().isoformat()}"


def _memory_block(ctx: TurnContext) -> str:
    memory_text = "\n".join(ctx.memory_index) if ctx.memory_index else ""
    mem_block = f"{MEMORY_INDEX_HEADING}{memory_text}" if memory_text else ""
    return mem_block


def _skill_line(skill: SkillRef, *, with_description: bool = True) -> str:
    description = " ".join(skill.description.split()) if with_description else ""
    if len(description) > _MAX_SKILL_DESCRIPTION_CHARS:
        description = description[: _MAX_SKILL_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return f"- {skill.name} — {description}" if description else f"- {skill.name}"


def _skills_section(ctx: TurnContext) -> str:
    skill_lines: list[str] = []
    used = 0
    for s in ctx.skills:
        line = _skill_line(s)
        if used + len(line) > _MAX_SKILL_DESCRIPTIONS_TOTAL_CHARS:
            line = _skill_line(s, with_description=False)
        else:
            used += len(line)
        skill_lines.append(line)
    skills_block = "\n".join(skill_lines)
    return f"{SKILLS_HEADING}{skills_block}" if skills_block else ""


def _invoked_skill_section(ctx: TurnContext) -> str:
    skill = ctx.invoked_skill
    if skill is None:
        return ""
    return (
        f"{INVOKED_SKILL_HEADING}"
        f"The user explicitly invoked `{skill.name}`. "
        "Use `list_skills` to get the skills root, then `read_file` that skill's "
        f"`SKILL.md` (`skills/{skill.name}/SKILL.md`) and follow it before acting. "
        "Text after the `/` command is the instruction for this invocation."
    )


def _background_tasks_section(ctx: TurnContext) -> str:
    view = ctx.background_runs_view
    if view is None:
        return ""
    try:
        body = view()
    except Exception:
        return ""
    if not isinstance(body, str) or not body.strip():
        return ""
    clipped = clip_lines(
        body.strip(),
        SECTION_CHAR_CAPS["background_tasks"],
        note="… [truncated]",
    )
    return f"{BACKGROUND_TASKS_HEADING}{clipped}\n"


def _todo_line(item_id: str, status: str, text: str) -> str:
    return f"- {item_id} [{status}] {text}"


def _pending_text(text: str, budget: int) -> str:
    """Pending item text clipped to ``budget`` characters, with an ellipsis when cut."""
    if len(text) <= budget:
        return text
    if budget <= 1:
        return _ELLIPSIS
    return text[: budget - 1] + _ELLIPSIS


def _join_under_cap(parts: list[str], cap: int) -> str | None:
    if not parts:
        return ""
    text = "\n".join(parts)
    if len(text) <= cap:
        return text
    return None


def _drop_todo_lines(item_lines: list[str], tail_lines: list[str], cap: int) -> str:
    """Drop item lines from the end until the block fits, and say how many went."""
    best: str | None = None
    lo, hi = 0, len(item_lines)
    while lo <= hi:
        mid = (lo + hi) // 2
        hidden = len(item_lines) - mid
        parts = [*item_lines[:mid], *tail_lines]
        if hidden:
            parts.append(f"- (+{hidden} more not shown)")
        fitted = _join_under_cap(parts, cap)
        if fitted is not None:
            best = fitted
            lo = mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best
    if tail_lines:
        return _drop_todo_lines(item_lines, [], cap)
    if not item_lines:
        return ""
    hidden = max(len(item_lines) - 1, 0)
    note = f"- (+{hidden} more not shown)" if hidden else "… [truncated]"
    return clip_lines(item_lines[0], cap, note=note)


def _render_todo_lines(items: Sequence[TodoItem], *, cap: int) -> str:
    """Store lines, shortened only when they would pass ``cap``.

    Under the cap the text matches ``TodoListStore.format_lines``. Over it, done
    items collapse to one line of ids, pending text is clipped to an equal share,
    and the in-progress item stays whole. A hand-edited list that is still too
    long drops lines from the end.
    """
    if not items:
        return ""
    full = "\n".join(_todo_line(item.id, item.status, item.text) for item in items)
    if len(full) <= cap:
        return full

    done = [item for item in items if item.status == "done"]
    open_items = [item for item in items if item.status != "done"]
    done_line = ""
    if done:
        ids = ", ".join(item.id for item in done)
        done_line = f"- done ({len(done)}): {ids}"

    def assemble(text_budget: int | None) -> tuple[str, int]:
        clipped = 0
        lines: list[str] = []
        for item in open_items:
            text = item.text
            if item.status != "in_progress" and text_budget is not None and len(text) > text_budget:
                clipped += 1
                text = _pending_text(text, text_budget)
            lines.append(_todo_line(item.id, item.status, text))
        if done_line:
            lines.append(done_line)
        if clipped:
            lines.append(_TODO_CLIP_NOTE)
        return "\n".join(lines), clipped

    collapsed, _clipped = assemble(None)
    if len(collapsed) <= cap:
        return collapsed

    pending_lengths = [len(item.text) for item in open_items if item.status != "in_progress"]
    longest = max(pending_lengths, default=0)
    best: str | None = None
    lo, hi = 0, longest
    while lo <= hi:
        mid = (lo + hi) // 2
        rendered, _clipped = assemble(mid)
        if len(rendered) <= cap:
            best = rendered
            lo = mid + 1
        else:
            hi = mid - 1
    if best is not None:
        return best

    item_lines = [
        _todo_line(
            item.id,
            item.status,
            item.text if item.status == "in_progress" else _pending_text(item.text, 0),
        )
        for item in open_items
    ]
    tail = [line for line in (done_line, _TODO_CLIP_NOTE) if line]
    return _drop_todo_lines(item_lines, tail, cap)


def _todo_list_section(ctx: TurnContext) -> str:
    store = ctx.todo_store
    if store is None:
        return ""
    lines = _render_todo_lines(store.items, cap=SECTION_CHAR_CAPS["todos"])
    return f"{TODO_LIST_HEADING}{lines}" if lines else ""


def _harness_text(ctx: TurnContext) -> str:
    include_task = any(t.name == "task" for t in ctx.tools)
    return harness_fixed_context(
        include_task_tool=include_task,
        memory_on=ctx.memory is not None,
        workspace_root=str(ctx.workspace_root) if ctx.workspace_root is not None else "(not set)",
        memory_storage_uri=ctx.memory.uri if ctx.memory is not None else "(not set)",
        run_command_opensandbox=SandboxConfig.from_env(ctx.config).enabled,
        subagent_personas=ctx.subagent_personas,
        emission_style=emission_style_terse_from_env(ctx.config),
        catalog_mcp_servers=ctx.catalog_mcp_servers,
    )


def _coding_context_blocks(ctx: TurnContext) -> str:
    """Coding-mode project instructions + repo overview (stable, so they cache with the prefix)."""
    out = ""
    if ctx.project_instructions:
        out += f"{PROJECT_INSTRUCTIONS_HEADING}{ctx.project_instructions}"
    if ctx.repo_overview:
        out += f"{REPO_OVERVIEW_HEADING}{ctx.repo_overview}"
    return out


def compose_stable_baseline(
    ctx: TurnContext,
    *,
    attachment_catalog: Sequence[AttachmentRecord] | None = None,
) -> str:
    """Cacheable prefix: AGENT.md + harness + project context + session attachments."""
    harness = _harness_text(ctx)
    coding_context = _coding_context_blocks(ctx)
    attachments = _session_attachments_block(attachment_catalog)
    return f"{ctx.agent_md}\n\n{harness}{coding_context}{attachments}"


def compose_volatile_tail(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
) -> str:
    """Volatile tail: current date + memory index + skills + invoked skill + current-request anchor."""
    return "".join(compose_volatile_tail_parts(ctx, chat_messages=chat_messages).values())


def compose_volatile_tail_parts(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
) -> dict[str, str]:
    """Same sections as :func:`compose_volatile_tail`, individually named.

    Lets callers (e.g. ``ContextEpochTracker``) attribute a mid-epoch volatile
    change to the specific source that moved — current date, memory, skills,
    invoked skill, todo list, or the current-request anchor — instead of a catch-all "volatile" label.
    """
    return {
        "current_date": _current_date_block(),
        "memory": _memory_block(ctx),
        "skills": _skills_section(ctx),
        "invoked_skill": _invoked_skill_section(ctx),
        "todos": _todo_list_section(ctx),
        "background_tasks": _background_tasks_section(ctx),
        "current_request": _current_request_block(chat_messages),
    }


def compose_system_prompt(
    ctx: TurnContext,
    *,
    chat_messages: Sequence[Message] | None = None,
    attachment_catalog: Sequence[AttachmentRecord] | None = None,
) -> str:
    """Build the system string: AGENT.md, harness, attachments, then volatile tail.

    ``ctx.agent_md`` is the operator-authored base prompt (typically from AGENT.md).
    Stable sections (harness, attachments) precede volatile curation (current date,
    memory, skills, todo list, current-request anchor) so implicit and explicit prompt caching
    can hit a contiguous prefix across turns.

    Skill names are always taken from ``ctx.skills`` (zero-cost discovery); use
    ``list_skills``/``read_file`` for the skills root path and full ``SKILL.md`` procedure.
    An explicit ``/slug`` invocation is listed under ``## Invoked skill`` for this turn.
    """
    stable = compose_stable_baseline(ctx, attachment_catalog=attachment_catalog)
    volatile = compose_volatile_tail(ctx, chat_messages=chat_messages)
    return f"{stable}{volatile}"
