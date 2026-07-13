"""Agent-facing workspace root resolution (shared by gateway and library entry points)."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.layout import resolve_agent_path, resolve_agent_root


def resolve_agent_workspace_root() -> Path:
    """Directory the agent should treat as its writable workspace.

    Resolution order:

    1. ``MONKEYBOT_WORKSPACE_ROOT`` — absolute path, or relative to agent root.
    2. ``WORKSPACE_ROOT`` — same semantics (legacy serverless alias).
    3. ``<agent-root>/workspace``.
    """
    import os

    root = resolve_agent_root()
    for env_name in ("MONKEYBOT_WORKSPACE_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return resolve_agent_path(raw, root)
    return (root / "workspace").resolve()
