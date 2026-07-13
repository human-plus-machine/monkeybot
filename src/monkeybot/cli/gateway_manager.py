"""Shim — implementation lives in ``monkeybot_cli.realtime.gateway_manager``."""

from __future__ import annotations

from monkeybot_cli.realtime.gateway_manager import (  # noqa: F401
    _find_workspace_dir,
    _url_is_local,
    start_gateway_if_needed,
    stop_gateway,
)

__all__ = [
    "_find_workspace_dir",
    "_url_is_local",
    "start_gateway_if_needed",
    "stop_gateway",
]
