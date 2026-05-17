"""Pluggable workspace storage (memory tree backends)."""

from __future__ import annotations

from monkeybot.core.workspace.factory import create_workspace_storage
from monkeybot.core.workspace.protocol import WorkspaceStorage

__all__ = ["WorkspaceStorage", "create_workspace_storage"]
