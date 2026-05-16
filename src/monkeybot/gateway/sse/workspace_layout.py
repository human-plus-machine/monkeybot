"""Agent-facing workspace root (playground file API + loop ``workspace_root``)."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_agent_workspace_root() -> Path:
    """Directory the agent should treat as its workspace (``data/``, ``skills/``, spill, …).

    Resolution:

    1. ``MONKEYBOT_WORKSPACE_ROOT`` — absolute path, or relative to process cwd.
    2. Else if ``<cwd>/workspace`` exists and is a directory, use it (local + Docker when cwd is
       the agent dir and ``workspace/`` holds only agent content).
    3. Else process cwd (backward compatible for tests and layouts without ``workspace/``).

    Cloud Run playground image sets ``WORKDIR`` to ``/app/workspace`` with only ``data`` and
    ``skills`` there, so step 2 does not apply and step 3 yields the slim tree.
    """
    raw = os.environ.get("MONKEYBOT_WORKSPACE_ROOT", "").strip()
    if raw:
        p = Path(raw)
        return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    nested = (Path.cwd() / "workspace").resolve()
    if nested.is_dir():
        return nested
    return Path.cwd().resolve()
