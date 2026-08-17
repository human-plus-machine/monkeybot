"""Workspace screenshot paths for browser_screenshot (vision / load_file bridge)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

_SCREENSHOTS_REL = Path("browser") / "Screenshots"


def workspace_root() -> Path:
    """monkeybot workspace root (for workspace-relative paths returned to the agent)."""
    for env_name in ("MONKEYBOT_WORKSPACE_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            p = Path(raw).expanduser()
            p = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()
            return p
    return (Path.cwd() / "workspace").resolve()


def screenshots_dir() -> Path:
    """Absolute directory where browser PNG captures are written."""
    raw = os.environ.get("BROWSER_MCP_SCREENSHOTS_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        p = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()
        return p
    return (workspace_root() / _SCREENSHOTS_REL).resolve()


def workspace_relative(abs_path: Path) -> str:
    """Return a workspace-relative path suitable for ``load_file`` / ``read_file``."""
    resolved = abs_path.resolve()
    root = workspace_root()
    try:
        rel = resolved.relative_to(root)
        return f"./{rel.as_posix()}"
    except ValueError:
        shots = screenshots_dir()
        try:
            rel = resolved.relative_to(shots)
            return f"./{_SCREENSHOTS_REL.as_posix()}/{rel.as_posix()}"
        except ValueError:
            return f"./{_SCREENSHOTS_REL.as_posix()}/{resolved.name}"


def allocate_screenshot_path() -> tuple[Path, str]:
    """Create a unique PNG path under the screenshots directory."""
    shots_dir = screenshots_dir()
    shots_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"shot-{ts}-{uuid.uuid4().hex[:8]}.png"
    abs_path = shots_dir / name
    return abs_path, workspace_relative(abs_path)
