"""Project instructions and the skills list the prompt composer reads.

Production puts ``load_project_instructions`` and ``repo_overview`` on the turn,
then lists an explicit ``/slug`` under ``## Invoked skill``. Real executors
already mount the fixture skills tree. Recording executors mount it here so
the same discovery can resolve ``/diagnostics`` with no workspace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from monkeybot.core.context import SkillRef, TurnContext, _discover_skills
from monkeybot.core.context.project_context import load_project_instructions, repo_overview
from regression.drivers.bundles.protocol import Bundle, RunCtx
from regression.drivers.bundles.session import _mount_skills, _resolve_skills_path
from regression.drivers.support.executor import _ObservedExecutor


def apply_project_context(
    turn: TurnContext,
    *,
    project_instructions: str,
    repo_overview: str,
) -> TurnContext:
    """Copy the coding-context fields onto the turn. The composer adds the headings."""
    if turn.project_instructions == project_instructions and turn.repo_overview == repo_overview:
        return turn
    return replace(
        turn,
        project_instructions=project_instructions,
        repo_overview=repo_overview,
    )


def message_skills(
    executor: Any,
    workdir: Path,
    merged: Mapping[str, Any],
    env: Mapping[str, str],
) -> list[SkillRef]:
    """Discover skills for this user message.

    Real executors retarget when ``paths.skills_path`` changes. Recording
    executors read the tree mounted in ``prepare``. ``apply_invoked_skill``
    then attaches a ``/slug`` for this turn only.
    """
    skills_path = _resolve_skills_path(workdir, merged, env)
    if isinstance(executor, _ObservedExecutor):
        executor.retarget_skills(skills_path)
    return _discover_skills(skills_path)


class _PromptContextBundle(Bundle):
    order = 45

    async def prepare(self, ctx: RunCtx) -> None:
        if ctx.workspace is not None:
            names = ctx.cfg.coding.project_instructions_files
            ctx.project_instructions = load_project_instructions(ctx.workspace, names)
            ctx.repo_overview = repo_overview(ctx.workspace)
        if ctx.scenario.executor != "real":
            _mount_skills(ctx.workdir)


BUNDLE = _PromptContextBundle()
