"""Fixture workspace copy, hostile overlay, and workspace caps."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.drivers.support.paths import _FIXTURE_PREFIXES, _WORKSPACE_ROOTS
from regression.fixtures.build import HOSTILE_SOURCE, overlay_hostile
from regression.schema.scenario import Scenario
from regression.schema.steps import TouchFileStep


def resolve_workspace(name: str) -> Path:
    """Find a fixture under the regression tree, then ``evals/fixtures/coding``.

    ``calc_repo``, ``calc_repo_green``, ``shop_repo``, and ``overlays/...``
    still live in the evals tree until P4-01. A ``fixtures/coding/`` prefix
    is stripped so either spelling resolves.
    """
    relative = name.strip().replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise LoopDriverError(f"workspace path must stay inside the fixture trees: {name!r}")
    for prefix in _FIXTURE_PREFIXES:
        if relative.startswith(prefix):
            relative = relative[len(prefix) :]
            break
    if not relative or ".." in Path(relative).parts:
        raise LoopDriverError(f"workspace path must stay inside the fixture trees: {name!r}")
    for root in _WORKSPACE_ROOTS:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(root) for root in _WORKSPACE_ROOTS)
    raise LoopDriverError(f"workspace fixture {name!r} was not found under {searched}")


def _is_hostile_source(path: Path) -> bool:
    return path.resolve() == HOSTILE_SOURCE.resolve()


def _overlay_hostile_workspace(workspace: Path, workdir: Path) -> None:
    """Add the generated hostile pieces. ``link_out`` resolves outside the copy."""
    outside = workdir / "outside-hostile"
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "link_target.txt"
    target.write_text("outside the hostile workspace\n", encoding="utf-8")
    overlay_hostile(workspace, link_target=target)


def _workspace_int_cap(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise LoopDriverError(f"{key} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise LoopDriverError(f"{key} must be >= 0, got {value}")
    return value


def _apply_workspace_env_caps(executor: CoreToolExecutor, env: Mapping[str, str]) -> None:
    """Honor scenario workspace caps the executor does not read from env.

    The executor keeps the object returned by the cached YAML loader. Replace
    it so one scenario's cap cannot leak into the next.
    """
    updates: dict[str, object] = {}
    if "WORKSPACE_WRITE_SCOPE_REL" in env:
        scope = env["WORKSPACE_WRITE_SCOPE_REL"].strip()
        updates["WORKSPACE_WRITE_SCOPE_REL"] = scope or None
    for key in ("WORKSPACE_WRITE_MAX_BYTES", "WORKSPACE_GLOB_MAX_PATHS"):
        if key not in env:
            continue
        parsed = _workspace_int_cap(env, key)
        if parsed is not None:
            updates[key] = parsed
    if not updates:
        return
    executor._workspace._settings = replace(executor._workspace._settings, **updates)


def _materialize_workspace(scenario: Scenario, workdir: Path) -> tuple[Path | None, Path | None]:
    wants_tree = scenario.workspace_fixture is not None or bool(scenario.workspace_overlays)
    if not wants_tree and scenario.executor != "real":
        return None, None
    workspace = workdir / "workspace"
    hostile = False
    if scenario.workspace_fixture is not None:
        source = resolve_workspace(scenario.workspace_fixture)
        # Keep store leaves that are symlinks. Following one would copy the
        # outside target into the workspace, and the attachment store would
        # then treat that leaf as a real file.
        shutil.copytree(source, workspace, symlinks=True)
        hostile = _is_hostile_source(source)
    else:
        workspace.mkdir()
    for overlay_name in scenario.workspace_overlays:
        source = resolve_workspace(overlay_name)
        shutil.copytree(source, workspace, dirs_exist_ok=True, symlinks=True)
        hostile = hostile or _is_hostile_source(source)
    if hostile:
        _overlay_hostile_workspace(workspace, workdir)
    if not wants_tree:
        return workspace, None
    pristine = workdir / "pristine"
    # Keep ``link_out`` a symlink. Following it would copy the outside target
    # into the snapshot as a regular file.
    shutil.copytree(workspace, pristine, symlinks=True)
    return workspace, pristine


class _WorkspaceBundle(Bundle):
    order = 10

    async def prepare(self, ctx: RunCtx) -> None:
        ctx.workspace, ctx.pristine = _materialize_workspace(ctx.scenario, ctx.workdir)
        if ctx.workspace is None and any(
            isinstance(step, TouchFileStep) for step in ctx.scenario.steps
        ):
            ctx.workspace = ctx.workdir / "workspace"
            ctx.workspace.mkdir()


BUNDLE = _WorkspaceBundle()
