"""Workspace screenshot paths for browser_screenshot (vision / render_image bridge)."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

_SCREENSHOTS_REL = Path("browser") / "Screenshots"


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def workspace_root() -> Path:
    """monkeybot workspace root (for workspace-relative paths returned to the agent)."""
    for env_name in ("MONKEYBOT_WORKSPACE_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            else:
                p = p.resolve()
            return p
    return (Path.cwd() / "workspace").resolve()


def screenshots_dir() -> Path:
    """Absolute directory where browser PNG captures are written."""
    raw = os.environ.get("BROWSER_MCP_SCREENSHOTS_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        return p
    return (workspace_root() / _SCREENSHOTS_REL).resolve()


def workspace_relative(abs_path: Path) -> str:
    """Return a workspace-relative path suitable for ``render_image`` / ``read_file``."""
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
    _prune_screenshots(shots_dir)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"shot-{ts}-{uuid.uuid4().hex[:8]}.png"
    abs_path = shots_dir / name
    return abs_path, workspace_relative(abs_path)


def _prune_screenshots(shots_dir: Path) -> None:
    """Bound workspace screenshot growth, deleting oldest captures first."""
    max_files = _positive_int_env("BROWSER_MCP_SCREENSHOTS_MAX_FILES", 200)
    max_bytes = _positive_int_env("BROWSER_MCP_SCREENSHOTS_MAX_BYTES", 100 * 1024 * 1024)
    files = sorted(
        (path for path in shots_dir.glob("*.png") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    total = sum(path.stat().st_size for path in files)
    # Zero disables the corresponding cap. Keep one slot free before a new
    # capture so a positive ``max_files`` remains an actual upper bound.
    while files and (
        (max_files > 0 and len(files) >= max_files)
        or (max_bytes > 0 and total > max_bytes)
    ):
        victim = files.pop(0)
        try:
            total -= victim.stat().st_size
            victim.unlink()
        except OSError:
            continue
