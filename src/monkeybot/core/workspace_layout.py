"""Agent-facing workspace root resolution (shared by gateway and library entry points)."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.layout import resolve_agent_path, resolve_agent_root, resolve_config_path


def resolve_agent_workspace_root() -> Path:
    """Directory the agent should treat as its writable workspace.

    ``monkeybot.yaml`` ``paths.workspace_root`` is the source of truth. When that
    key is absent, falls back to ``<agent-root>/workspace``.
    """
    from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

    root = resolve_agent_root()
    cfg = resolve_config_path(agent_root=root)
    _, doc = load_monkeybot_yaml_dict(cfg)
    paths = doc.get("paths") if isinstance(doc, dict) else None
    if isinstance(paths, dict):
        raw = paths.get("workspace_root")
        if isinstance(raw, str) and raw.strip():
            return resolve_agent_path(raw.strip(), root)
    return (root / "workspace").resolve()
