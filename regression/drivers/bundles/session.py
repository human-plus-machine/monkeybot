"""Skills mount, todo list, scheduled-loop store, and memory subsystem."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from monkeybot.core.config.settings import get_subagent_registry
from monkeybot.core.context import TurnContext
from monkeybot.core.context.slash_skills import apply_invoked_skill
from monkeybot.core.hooks import HookManager
from monkeybot.core.layout import resolve_agent_path
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.backends import ScheduledLoopStore
from monkeybot.core.persistence.scheduled_loops import KIND_GOAL, KIND_LOOP
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.read_ledger import FileReadLedger
from monkeybot.core.tools.workspace_service import WorkspaceError
from monkeybot.todo_list import TodoListStore, TodoListTool
from regression.drivers.bundles.hooks import register_memory_index
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.drivers.bundles.workspace import _apply_workspace_env_caps
from regression.drivers.support.executor import _ObservedExecutor, _recording_executor, _shutdown
from regression.drivers.support.paths import (
    _GOAL_SKILL,
    _OFF_VALUES,
    _PATCH_FAIL_ENV,
    _SESSION_ID,
    _SESSION_ID_ENV,
)
from regression.fixtures.build import SKILLS_SOURCE, overlay_skills
from regression.schema.assertions import StoreView
from regression.schema.scenario import PalaceDrawer, Scenario
from testkit.mcp import NoMCP
from testkit.palace import InMemoryPalace


def _take_session_id(env: dict[str, str]) -> str:
    """Session id for this run. ``..`` is allowed so spill sanitization can be seen.

    Popped here so the key never becomes a process environment variable.
    """
    raw = env.pop(_SESSION_ID_ENV, None)
    if raw is None:
        return _SESSION_ID
    text = raw.strip()
    if not text or "\x00" in text:
        raise LoopDriverError(f"{_SESSION_ID_ENV} must be a non-empty session id")
    return text


def _install_patch_rollback_fault(executor: CoreToolExecutor, env: dict[str, str]) -> None:
    """Fail the second patch write and every rollback delete.

    ``MB_LOOP_PATCH_FAIL=rollback`` is the injected mid-apply failure T2-FILE-19
    describes. The first write lands; the next write raises; deleting that new
    file during rollback also raises, so the product reports ``rollback_failed``
    and the dirty path.
    """
    raw = env.pop(_PATCH_FAIL_ENV, None)
    if raw is None:
        return
    mode = raw.strip()
    if mode != "rollback":
        raise LoopDriverError(f"{_PATCH_FAIL_ENV} must be 'rollback' when set, not {mode!r}")
    workspace = executor._workspace
    real_write = workspace.write_file
    writes = {"n": 0}

    def flaky_write(path: str, content: str):
        writes["n"] += 1
        if writes["n"] >= 2:
            raise WorkspaceError("disk full", code="write_failed")
        return real_write(path, content)

    def flaky_delete(path: str):
        del path
        raise WorkspaceError("delete blocked", code="write_failed")

    workspace.write_file = flaky_write  # type: ignore[method-assign]
    workspace.delete_file = flaky_delete  # type: ignore[method-assign]


def _scenario_opt_out(
    merged: Mapping[str, Any],
    env: Mapping[str, str],
    env_key: str,
    section: str,
    field: str,
) -> bool:
    """True when the scenario document turns a default-on flag off.

    Process pins are ignored so the host environment cannot disable the todo
    tool or its disk mirror for these scenarios.
    """
    raw: object = env.get(env_key)
    if raw is None:
        block = merged.get(section)
        if isinstance(block, dict) and field in block:
            raw = block[field]
        elif f"{section}.{field}" in merged:
            raw = merged[f"{section}.{field}"]
    if raw is None:
        return False
    return str(raw).strip().lower() in _OFF_VALUES


def _scenario_memory_disabled(scenario: Scenario) -> bool:
    """True when this scenario turns memory off.

    ``agent.base.yaml`` also says ``memory.enabled: false``. Scenarios that
    do not set the flag still receive a subsystem, so a memory-on case stays
    on. An explicit false (``memory.enabled`` or ``MONKEYBOT_MEMORY_HOOK_ENABLED``
    on the scenario) passes ``memory=None`` and mempalace is not spawned.
    """
    if scenario.config is None:
        return False
    dumped = scenario.config.model_dump()
    raw: object = None
    env = dumped.get("env")
    if isinstance(env, dict) and "MONKEYBOT_MEMORY_HOOK_ENABLED" in env:
        raw = env["MONKEYBOT_MEMORY_HOOK_ENABLED"]
    if raw is None:
        memory = dumped.get("memory")
        if isinstance(memory, dict) and "enabled" in memory:
            raw = memory["enabled"]
        elif "memory.enabled" in dumped:
            raw = dumped["memory.enabled"]
    if raw is None:
        return False
    return str(raw).strip().lower() in _OFF_VALUES


def _configured_skills_raw(merged: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """Skills path from the scenario document, not a process ``SKILLS_PATH`` pin."""
    raw = env.get("SKILLS_PATH", "")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    paths = merged.get("paths")
    if isinstance(paths, dict):
        value = paths.get("skills_path")
        if isinstance(value, str) and value.strip():
            return value.strip()
    dotted = merged.get("paths.skills_path")
    if isinstance(dotted, str) and dotted.strip():
        return dotted.strip()
    return None


def _resolve_skills_path(workdir: Path, merged: Mapping[str, Any], env: Mapping[str, str]) -> Path:
    raw = _configured_skills_raw(merged, env)
    if raw is None:
        return (workdir / "skills").resolve()
    return resolve_agent_path(raw, workdir)


def _mount_skills(workdir: Path) -> Path:
    """Copy the fixture skills tree and add the escape symlink.

    Discovery is immediate ``SKILL.md`` directories only, so ``diagnostics`` and
    ``plain`` are visible and ``a/nested`` is not. ``escape`` resolves outside
    the skills root.
    """
    skills = workdir / "skills"
    if skills.exists():
        shutil.rmtree(skills)
    shutil.copytree(SKILLS_SOURCE, skills)
    outside = workdir / "outside-skills"
    outside.mkdir()
    target = outside / "skill_target.txt"
    target.write_text("outside the skills root\n", encoding="utf-8")
    overlay_skills(skills, link_target=target)
    return skills.resolve()


def _apply_user_slash(ctx: TurnContext, message: str) -> TurnContext:
    """Resolve ``/slug`` the way the gateway does, including ``/goal``.

    The fixture tree has no ``goal`` directory: skill prompts require
    ``diagnostics`` then ``plain`` with nothing between them. ``/goal`` still
    resolves against the scaffold skill so ``create_goal`` sees an invoked
    skill named goal. That name is not added to the prompt skills list.
    """
    discovered = list(ctx.skills)
    include_goal = ctx.scheduled_loops_available and all(
        skill.name.lower() != "goal" for skill in discovered
    )
    lookup = [*discovered, _GOAL_SKILL] if include_goal else discovered
    applied = apply_invoked_skill(replace(ctx, skills=lookup), message)
    if include_goal:
        return replace(applied, skills=discovered)
    return applied


async def _store_snapshot(store: ScheduledLoopStore) -> StoreView:
    loops = await store.list_kind(KIND_LOOP)
    goals = await store.list_kind(KIND_GOAL)
    return StoreView(loops=len(loops), goals=len(goals))


def _scenario_sqlite_url(ctx: RunCtx) -> str | None:
    """SQLite ``DB_URL`` or ``paths.db_url`` from the scenario, if it set one.

    A Postgres or Firestore URL is not an outbox path for this subsystem.
    The memory bundle owns the shared-outbox boot check for those schemes.
    """
    raw = ctx.env.get("DB_URL", "").strip()
    if not raw:
        paths = ctx.merged.get("paths")
        if isinstance(paths, dict) and isinstance(paths.get("db_url"), str):
            raw = paths["db_url"].strip()
    if raw.startswith("sqlite:"):
        return raw
    return None


def _memory_subsystem(
    root: Path,
    drawers: Sequence[PalaceDrawer],
    *,
    db_url: str | None = None,
) -> MemorySubsystem:
    root.mkdir(parents=True, exist_ok=True)
    palace_path = root / "mempalace"
    palace = InMemoryPalace(palace_path, agent_name="loop-scenario")
    for drawer in drawers:
        metadata = dict(drawer.metadata)
        metadata.setdefault("wing", drawer.wing)
        metadata.setdefault("room", drawer.room)
        if drawer.filed_at is not None:
            metadata["filed_at"] = drawer.filed_at
        palace.upsert_drawer(drawer.drawer_id, drawer.content, metadata)
    return MemorySubsystem(
        memory_uri=f"local://{palace_path}",
        db_url=db_url or f"sqlite:///{root / 'monkeybot.db'}",
        agent_id="loop-scenario",
        agent_name="loop-scenario",
        palace=palace,
        writer_enabled=True,
    )


class _SessionBundle(Bundle):
    order = 40
    hooks = ("memory",)

    async def prepare(self, ctx: RunCtx) -> None:
        ctx.session_id = _take_session_id(ctx.env)
        drawers = list(ctx.scenario.memory.palace) if ctx.scenario.memory is not None else []
        if "memory" in ctx.hook_names and not _scenario_memory_disabled(ctx.scenario):
            root = ctx.workdir / "memory"
            ctx.hook_memory = _memory_subsystem(root, drawers, db_url=_scenario_sqlite_url(ctx))
        if ctx.scenario.executor != "real":
            if _PATCH_FAIL_ENV in ctx.env:
                raise LoopDriverError(f"{_PATCH_FAIL_ENV} needs executor: real")
            ctx.executor = _recording_executor(ctx.scenario)
            return
        if ctx.workspace is None:
            raise LoopDriverError("executor real needs a workspace directory")
        _mount_skills(ctx.workdir)
        skills_path = _resolve_skills_path(ctx.workdir, ctx.merged, ctx.env)
        ctx.backend = SQLiteStorageBackend(f"sqlite:///{ctx.workdir / 'loops.db'}")
        await ctx.backend.open()
        ctx.loop_store = ctx.backend.scheduled_loops()
        extra_tools: list[Any] = list(ctx.host_tools)
        if not _scenario_opt_out(
            ctx.merged, ctx.env, "MONKEYBOT_TODO_LIST_ENABLED", "todo_list", "enabled"
        ):
            ctx.todo_store = TodoListStore(
                ctx.session_id,
                workspace_root=ctx.workspace,
                mirror_to_disk=not _scenario_opt_out(
                    ctx.merged,
                    ctx.env,
                    "MONKEYBOT_TODO_LIST_MIRROR_TO_DISK",
                    "todo_list",
                    "mirror_to_disk",
                ),
            )
            await ctx.todo_store.hydrate_from_disk()
            todo_tool = TodoListTool(ctx.todo_store)
            extra_tools.append(todo_tool)
            ctx.extra_tool_defs.append(todo_tool.tool_def)
        ctx.read_ledger = FileReadLedger()
        if _scenario_memory_disabled(ctx.scenario):
            tool_memory = None
        else:
            tool_memory = ctx.hook_memory or _memory_subsystem(ctx.workdir / "tool-memory", [])
        policy_kwargs: dict[str, Any] = {}
        policy = ctx.policy
        if policy is not None and policy.grants_path is not None:
            policy_kwargs["grants_path"] = policy.grants_path
        if policy is not None and policy.allowed_commands is not None:
            policy_kwargs["run_command_allowed_commands"] = policy.allowed_commands
            policy_kwargs["run_command_allowed_path_prefixes"] = policy.allowed_path_prefixes
        from monkeybot.core.subagents.supervisor import SubagentSupervisor
        from monkeybot.core.tools.core_tool_executor import _run_inline_subagent_with_progress
        from monkeybot.core.config.settings import get_subagent_settings

        settings = get_subagent_settings(config=ctx.cfg)
        supervisor = SubagentSupervisor(
            session_id=ctx.session_id,
            run_store=ctx.backend.runs() if ctx.backend is not None else None,
            max_background=settings.max_background,
            lease_ms=settings.lease_ms,
            run_inline=_run_inline_subagent_with_progress,
        )
        tool_executor = CoreToolExecutor(
            workspace_root=ctx.workspace,
            memory=tool_memory,
            skills_path=skills_path,
            mcp=ctx.mcp_client if ctx.mcp_client is not None else NoMCP(),
            extra_tools=extra_tools or None,
            attachment_store=ctx.attachment_store,
            scheduled_loop_store=ctx.loop_store,
            run_store=ctx.backend.runs() if ctx.backend is not None else None,
            subagent_registry=get_subagent_registry(config=ctx.cfg),
            subagent_supervisor=supervisor,
            read_ledger=ctx.read_ledger,
            config=ctx.cfg,
            **policy_kwargs,
        )
        _apply_workspace_env_caps(tool_executor, ctx.env)
        _install_patch_rollback_fault(tool_executor, ctx.env)
        ctx.executor = _ObservedExecutor(tool_executor)

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        # Index lines stay ahead of the memory hooks, which stay ahead of git.
        register_memory_index(manager, ctx)
        if ctx.hook_memory is not None:
            ctx.hook_memory.register_hooks(manager)

    async def close(self, ctx: RunCtx) -> None:
        if ctx.executor is None:
            if ctx.hook_memory is not None:
                await ctx.hook_memory.close()
                ctx.hook_memory = None
        else:
            await _shutdown(ctx.executor, ctx.hook_memory, ctx.mcp_client)
            ctx.mcp_client = None
            ctx.hook_memory = None
            ctx.executor = None
        if ctx.backend is not None:
            await ctx.backend.close()
            ctx.backend = None


BUNDLE = _SessionBundle()
