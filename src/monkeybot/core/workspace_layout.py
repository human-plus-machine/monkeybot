"""Agent-facing workspace root resolution (shared by gateway and library entry points)."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.layout import resolve_agent_root, resolve_config_path, resolve_workspace_root


def resolve_agent_workspace_root() -> Path:
    """Directory the agent should treat as its writable workspace.

    Honors absolute ``MONKEYBOT_WORKSPACE_ROOT_OVERRIDE`` when set (Mac workspace
    remapping). Otherwise ``monkeybot.yaml`` ``paths.workspace_root`` is the
    source of truth; when that key is absent, falls back to
    ``<agent-root>/workspace``.
    """
    root = resolve_agent_root()
    return resolve_workspace_root(agent_root=root, config_path=resolve_config_path(agent_root=root))
