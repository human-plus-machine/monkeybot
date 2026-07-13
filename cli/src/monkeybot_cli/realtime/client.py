"""Deprecated shim — use ``encode_client_frame`` and ``is_exit_command``."""

from __future__ import annotations

from monkeybot_cli.exit_commands import is_exit_command
from monkeybot_cli.realtime.wire_encode import encode_client_frame


class RealtimeClientError(RuntimeError):
    """Deprecated alias; prefer ``RuntimeError``."""


__all__ = [
    "RealtimeClientError",
    "encode_client_frame",
    "is_exit_command",
]
