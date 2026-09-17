"""Resolve a leading `/skill-slug` against the skills installed for this turn."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from monkeybot.core.context import GOAL_TOOL_DEFS, GOAL_TOOL_NAMES, SkillRef, TurnContext
from monkeybot.core.persistence.scheduled_loops import GOAL_CONTINUATION_PREFIX
from monkeybot.core.types.types_tools import ToolDef

_SLASH_TOKEN = re.compile(r"(?:^|\s)/([^\s]+)")


def resolve_invoked_skill(text: str, skills: Sequence[SkillRef]) -> SkillRef | None:
    """Match a `/slug` token against installed skill directory names."""
    by_name = {skill.name.lower(): skill for skill in skills}
    for match in _SLASH_TOKEN.finditer(text):
        skill = by_name.get(match.group(1).lower())
        if skill is not None:
            return skill
    return None


def apply_invoked_skill(ctx: TurnContext, user_text: str) -> TurnContext:
    """Attach a turn-scoped invoked skill without rewriting persisted user text."""
    skill = resolve_invoked_skill(user_text, ctx.skills)
    tools = _with_goal_tools(ctx, skill=skill, user_text=user_text)
    if skill is None and tools is ctx.tools:
        return ctx
    return replace(ctx, invoked_skill=skill, tools=tools)


def _with_goal_tools(
    ctx: TurnContext,
    *,
    skill: SkillRef | None,
    user_text: str,
) -> list[ToolDef]:
    """Advertise goal tools only on `/goal` invocations and goal continuation ticks."""
    if not ctx.scheduled_loops_available:
        return ctx.tools
    if skill is not None and skill.name.lower() == "goal":
        wanted = GOAL_TOOL_NAMES
    elif user_text.startswith(GOAL_CONTINUATION_PREFIX):
        # A tick resumes an existing goal, so only completion/resume is offered.
        wanted = frozenset({"update_goal"})
    else:
        return ctx.tools
    missing = wanted - {tool.name for tool in ctx.tools}
    if not missing:
        return ctx.tools
    return [*ctx.tools, *(tool for tool in GOAL_TOOL_DEFS if tool.name in missing)]
