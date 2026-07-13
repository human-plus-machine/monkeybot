"""Load and merge monkeybot.yaml documents (shared by runtime and subagent registry)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from monkeybot.core.layout import resolve_agent_root, resolve_config_path as resolve_layout_config_path
from monkeybot.core.config.runtime_env import _load_yaml_file, _merge_with_includes

logger = logging.getLogger(__name__)


def resolve_monkeybot_config_path(
    explicit: str | Path | None = None,
    *,
    cwd: Path | None = None,
) -> Path | None:
    """Resolve monkeybot.yaml path from explicit arg, MONKEYBOT_CONFIG, or default."""
    if explicit is not None:
        p = Path(explicit).expanduser()
        return p.resolve() if p.is_file() else None
    root = resolve_agent_root(cwd=cwd)
    resolved = resolve_layout_config_path(agent_root=root)
    if resolved is None and os.environ.get("MONKEYBOT_CONFIG", "").strip():
        logger.warning("MONKEYBOT_CONFIG is set but not a file: %s", os.environ["MONKEYBOT_CONFIG"])
    return resolved


def load_monkeybot_yaml_dict(config_path: str | Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Load merged monkeybot.yaml (with includes) as a dict."""
    path = resolve_monkeybot_config_path(config_path)
    if path is None:
        return None, {}
    root = _load_yaml_file(path)
    merged = _merge_with_includes(path, root)
    return path, merged
