"""Real policy inspectors loaded from fixture configs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from monkeybot.core.tools.grant_store import build_grants_persist_hook
from monkeybot.core.tools.inspector import CommandTierConfigError, CommandTierInspector, Decision
from monkeybot.core.tools.loop_inspector import LoopStartInspector
from monkeybot.core.tools.path_grant_inspector import PathGrantInspector
from monkeybot.core.tools.permission import try_load_permission_inspector
from monkeybot.core.verifier.inspector import VerifierInspector
from monkeybot.core.verifier.mailbox import VerdictMailbox
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.drivers.support.paths import _CONFIGS, _GRANT_FIXTURE, _REPO
from regression.drivers.support.tokens import _scenario_tokens
from regression.schema.scenario import Scenario
from regression.schema.steps import ConfirmToolStep, Step
from testkit.inspectors import AllowInspector


@dataclass
class _PolicyBundle:
    """Real inspectors loaded from a scenario's ``policy`` fixtures."""

    inspectors: list[Any]
    allowed_commands: tuple[str, ...] | None = None
    allowed_path_prefixes: tuple[str, ...] | None = None
    grants_path: Path | None = None
    grant_root: Path | None = None
    staged: list[Path] = field(default_factory=list)


def _policy_config_path(kind: str, name: str) -> Path:
    if kind == "grants":
        if name in {"default", "grants"}:
            return _CONFIGS / "grants.json"
        return _CONFIGS / f"grants.{name}.json"
    stem = "command_allowlist" if kind == "command_allowlist" else "permissions"
    return _CONFIGS / f"{stem}.{name}.yaml"


def _cleanup_policy(bundle: _PolicyBundle | None) -> None:
    if bundle is None:
        return
    for path in bundle.staged:
        shutil.rmtree(path, ignore_errors=True)
    bundle.staged.clear()


def _grants_persist(bundle: _PolicyBundle | None) -> Callable[[str, str], bool] | None:
    if bundle is None or bundle.grants_path is None:
        return None
    return build_grants_persist_hook(bundle.grants_path)


def _stage_grants(
    name: str,
    workdir: Path,
    workspace: Path | None,
    bundle: _PolicyBundle,
) -> None:
    """Copy the grants fixture somewhere the scenario can revoke, under ``$HOME``.

    The staged folder sits in this checkout so ``PathGrantInspector`` treats it as
    inside the home directory. ``link_out`` points at a workspace file, so a
    resolved symlink does not widen the grant. ``grants.json`` is written into
    the workspace when there is one, so ``touch_file`` can replace it.
    """
    source = _policy_config_path("grants", name)
    if not source.is_file():
        raise LoopDriverError(f"grants fixture {name!r} is missing ({source})")
    raw = source.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    dest_root = workspace if workspace is not None else workdir
    dest = dest_root / "grants.json"
    if not isinstance(parsed, dict):
        dest.write_text(raw, encoding="utf-8")
        bundle.grants_path = dest
        return
    paths = parsed.get("paths")
    if isinstance(paths, list) and paths:
        parent = Path(tempfile.mkdtemp(prefix="mb-grant-", dir=_REPO))
        bundle.staged.append(parent)
        granted = parent / "granted"
        if _GRANT_FIXTURE.is_dir():
            shutil.copytree(_GRANT_FIXTURE, granted)
        else:
            granted.mkdir()
        notes = granted / "notes.md"
        if not notes.is_file():
            notes.write_text("Notes the folder grant may read.\n", encoding="utf-8")
        env_file = granted / ".env"
        if not env_file.is_file():
            env_file.write_text("SECRET=not-a-real-secret\n", encoding="utf-8")
        target_root = workspace if workspace is not None else parent
        target = target_root / "symlink-target.txt"
        if not target.exists():
            target.write_text("OUTSIDE-GRANT\n", encoding="utf-8")
        link = granted / "link_out"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target.resolve(), link)
        grant_root = granted.resolve()
        rewritten: list[Any] = []
        for item in paths:
            if isinstance(item, dict):
                copied = dict(item)
                copied["path"] = str(grant_root)
                rewritten.append(copied)
            else:
                rewritten.append(item)
        parsed["paths"] = rewritten
        bundle.grant_root = grant_root
    dest.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    bundle.grants_path = dest


def _load_policy(
    scenario: Scenario,
    workdir: Path,
    workspace: Path | None,
) -> _PolicyBundle | None:
    """Gateway inspector order, from the scenario's fixture names.

    A missing permissions file is fail-open: ``try_load_permission_inspector``
    returns ``None`` and the command tier still applies. An empty allowlist
    still raises.
    """
    policy = scenario.policy
    if policy is None:
        return None
    bundle = _PolicyBundle(inspectors=[])
    try:
        if policy.grants:
            _stage_grants(policy.grants, workdir, workspace, bundle)
        if policy.command_allowlist:
            path = _policy_config_path("command_allowlist", policy.command_allowlist)
            if not path.is_file():
                raise LoopDriverError(
                    f"command allowlist fixture {policy.command_allowlist!r} is missing ({path})"
                )
            try:
                tier = CommandTierInspector(path, grants_path=bundle.grants_path)
            except CommandTierConfigError as exc:
                raise LoopDriverError(str(exc)) from exc
            bundle.inspectors.append(tier)
            bundle.allowed_commands = tier.allowed_commands
            bundle.allowed_path_prefixes = tier.allowed_path_prefixes
        root = workspace if workspace is not None else workdir
        bundle.inspectors.append(
            PathGrantInspector(workspace_root=root.resolve(), grants_path=bundle.grants_path)
        )
        if policy.permissions:
            loaded = try_load_permission_inspector(
                _policy_config_path("permissions", policy.permissions)
            )
            if loaded is not None:
                bundle.inspectors.append(loaded)
        bundle.inspectors.append(LoopStartInspector())
    except Exception:
        _cleanup_policy(bundle)
        raise
    return bundle


class _ScenarioConfirmInspector:
    """Confirm only the calls a ``confirm_tool`` step will answer."""

    def __init__(self, steps: Sequence[Step]) -> None:
        confirm_all = False
        ids: set[str] = set()
        for step in steps:
            if not isinstance(step, ConfirmToolStep):
                continue
            call_id = step.call_id or step.when.call_id
            if call_id is None:
                confirm_all = True
            else:
                ids.add(call_id)
        self._all = confirm_all
        self._ids = ids

    async def check(self, call: object, ctx: object) -> Decision:
        del ctx
        call_id = getattr(call, "call_id", None)
        if self._all or (isinstance(call_id, str) and call_id in self._ids):
            return Decision(kind="confirm", message="scenario confirmation")
        return Decision(kind="allow")


def _inspectors(
    scenario: Scenario,
    mailbox: VerdictMailbox | None = None,
    policy: _PolicyBundle | None = None,
    computer_inspector: Any = None,
) -> list[Any]:
    """Allow-all, or the real policy chain when the scenario names fixtures.

    A confirm step still answers the bus. It does not replace the real
    inspectors: those are what decide that a call needs confirmation.

    The computer inspector is first so a hard denial or the computer ask
    prompt wins before a scenario confirm step supplies its own message.
    """
    inspectors: list[Any] = []
    if computer_inspector is not None:
        inspectors.append(computer_inspector)
    if policy is not None:
        inspectors.extend(policy.inspectors)
    elif any(isinstance(step, ConfirmToolStep) for step in scenario.steps):
        inspectors.append(_ScenarioConfirmInspector(scenario.steps))
    else:
        inspectors.append(AllowInspector())
    if mailbox is not None:
        inspectors.append(VerifierInspector(mailbox))
    return inspectors


class _PolicyDriverBundle(Bundle):
    order = 20

    async def prepare(self, ctx: RunCtx) -> None:
        ctx.policy = _load_policy(ctx.scenario, ctx.workdir, ctx.workspace)
        ctx.tokens = _scenario_tokens(ctx.workspace, ctx.policy)

    async def close(self, ctx: RunCtx) -> None:
        _cleanup_policy(ctx.policy)


BUNDLE = _PolicyDriverBundle()
