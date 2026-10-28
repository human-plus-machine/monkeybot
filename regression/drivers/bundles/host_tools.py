"""Attachment store, fake web search, and computer tools for a real executor."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from monkeybot.computer import (
    build_computer_tools,
    should_enable_computer_tools,
)
from monkeybot.computer.permissions import (
    build_computer_permission_inspector,
    build_persist_hook,
)
from monkeybot.core.attachments.store import FilesystemAttachmentStore
from monkeybot.core.attachments.tools import load_file_tool_def
from monkeybot.core.config.snapshot import env_value
from monkeybot.web_search import WebSearchTool, build_backend
from regression.drivers.bundles.protocol import Bundle, RunCtx
from testkit.web_search import FakeWebSearchBackend

_HUGE_TRASH_ITEMS = 10_001


def _web_search_tool(ctx: RunCtx) -> WebSearchTool | None:
    """Install the backend the scenario names. Unset stays off.

    ``fake`` is harness-only. ``build_backend`` does not know that name, and
    a real backend would not return the titles these cases assert.
    """
    name = ctx.env.get("WEB_SEARCH_BACKEND", "").strip().lower()
    if not name or name == "none":
        return None
    if name == "fake":
        return WebSearchTool(FakeWebSearchBackend())
    backend = build_backend(ctx.cfg)
    if backend is None:
        return None
    return WebSearchTool(backend)


def _plant_huge_trash_tree(workspace: Path) -> Path:
    """A home-directory tree large enough for ``check_trashable``.

    ``computer_trash`` only counts items under the user's home. The scenario
    path is ``{workspace}/huge``; that directory is a symlink to this tree so
    the resolved path is inside home and the count still runs.
    """
    root = Path.home() / "Downloads" / f"mb-p1-17-comp06-{os.getpid()}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for index in range(_HUGE_TRASH_ITEMS):
        fd = os.open(root / f"{index}.txt", os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
    link = workspace / "huge"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(root, target_is_directory=True)
    return root


def _remove_huge_trash_tree(root: Path | None) -> None:
    if root is None:
        return
    shutil.rmtree(root, ignore_errors=True)
    trashed = Path.home() / ".Trash" / root.name
    if trashed.exists():
        shutil.rmtree(trashed, ignore_errors=True)


class _HostToolsBundle(Bundle):
    order = 35

    async def prepare(self, ctx: RunCtx) -> None:
        if ctx.scenario.executor != "real" or ctx.workspace is None:
            return
        if env_value(ctx.cfg, "ATTACHMENTS_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "",
        }:
            ctx.attachment_store = FilesystemAttachmentStore(ctx.workspace)
            ctx.extra_tool_defs.append(load_file_tool_def())
        search = _web_search_tool(ctx)
        if search is not None:
            ctx.host_tools.append(search)
            ctx.extra_tool_defs.append(search.tool_def)
        if should_enable_computer_tools(ctx.cfg):
            tools = build_computer_tools()
            ctx.host_tools.extend(tools)
            ctx.extra_tool_defs.extend(tool.tool_def for tool in tools)
            approvals = ctx.workdir / "computer-approvals.json"
            permissions = ctx.workdir / "computer-permissions.yaml"
            ctx.computer_inspector = build_computer_permission_inspector(permissions, approvals)
            ctx.approvals_persist = build_persist_hook(approvals)
        if ctx.scenario.case == "T2-COMP-06":
            ctx.huge_trash_root = _plant_huge_trash_tree(ctx.workspace)

    async def close(self, ctx: RunCtx) -> None:
        _remove_huge_trash_tree(ctx.huge_trash_root)
        ctx.huge_trash_root = None


BUNDLE = _HostToolsBundle()
