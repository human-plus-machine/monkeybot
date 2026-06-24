"""Agent-facing workspace root resolution (shared by gateway and library entry points)."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_agent_workspace_root() -> Path:
    """Directory the agent should treat as its workspace (``data/``, ``skills/``, spill, …).

    Resolution order:

    1. ``MONKEYBOT_WORKSPACE_ROOT`` — absolute path, or relative to process cwd.
    2. ``WORKSPACE_ROOT`` — same semantics (alias for serverless / legacy handlers).
    3. Else if ``<cwd>/workspace`` exists and is a directory, use it.
    4. Else process cwd.
    """
    for env_name in ("MONKEYBOT_WORKSPACE_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            p = Path(raw)
            return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    nested = (Path.cwd() / "workspace").resolve()
    if nested.is_dir():
        return nested
    return Path.cwd().resolve()
