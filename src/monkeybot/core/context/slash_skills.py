"""Resolve a leading `/skill-slug` against the skills installed for this turn."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from monkeybot.core.context import SkillRef, TurnContext

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
    if skill is None:
        return ctx
    return replace(ctx, invoked_skill=skill)
