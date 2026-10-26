"""Dirty-repo fixture and the git context hooks."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from monkeybot.core.coding.git_context_hook import GitContextHook, git_workspace_env
from monkeybot.core.coding.read_ledger_hook import ReadLedgerCompactionHook
from monkeybot.core.hooks import HookManager
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.drivers.support.paths import _GIT_FIXTURE_ENV, _WORKSPACE_TOKEN


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "-c", "safe.directory=*", *args],
        check=False,
        capture_output=True,
        env=git_workspace_env(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise LoopDriverError(f"git {' '.join(args)} failed in {repo}: {detail}")


def _init_dirty_repo(repo: Path, *, branch: str, filename: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    target = repo / filename
    target.write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "--", filename)
    _git(
        repo,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=tester",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "init",
    )
    target.write_text("v2\n", encoding="utf-8")


def _prepare_git_context(
    workspace: Path | None,
    env: dict[str, str],
    hook_names: Sequence[str],
) -> None:
    """Build repos the git-context hook will read, then drop the fixture env key.

    ``MB_LOOP_GIT_FIXTURE=dirty`` is a dirty workspace repo. A ``GIT_DIR`` that
    still contains ``{workspace}`` is a decoy under ``other/`` plus a different
    dirty workspace repo. Both are created with git's repo env stripped, before
    the scenario env is pushed, so setup itself is not retargeted.
    """
    if "git_context" not in hook_names or workspace is None:
        env.pop(_GIT_FIXTURE_ENV, None)
        return
    fixture = env.get(_GIT_FIXTURE_ENV, "").strip()
    git_dir = env.get("GIT_DIR", "")
    if fixture == "dirty":
        _init_dirty_repo(workspace, branch="main", filename="dirty.txt")
    if _WORKSPACE_TOKEN in git_dir:
        _init_dirty_repo(workspace, branch="workspace-branch", filename="only_workspace.py")
        _init_dirty_repo(workspace / "other", branch="other-branch", filename="only_other.py")
    elif fixture not in {"", "dirty"}:
        raise LoopDriverError(f"{_GIT_FIXTURE_ENV} must be 'dirty' when set, not {fixture!r}")
    env.pop(_GIT_FIXTURE_ENV, None)


class _GitBundle(Bundle):
    order = 50
    hooks = ("git_context", "read_ledger_compaction")

    async def prepare(self, ctx: RunCtx) -> None:
        _prepare_git_context(ctx.workspace, ctx.env, ctx.hook_names)

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        if "git_context" in ctx.hook_names:
            GitContextHook().register(manager)
        if "read_ledger_compaction" in ctx.hook_names:
            ReadLedgerCompactionHook().register(manager)


BUNDLE = _GitBundle()
