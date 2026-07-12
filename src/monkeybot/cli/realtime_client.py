"""Shim — encode helpers live in ``monkeybot_cli.realtime``."""

from __future__ import annotations

from monkeybot_cli.exit_commands import is_exit_command
from monkeybot_cli.realtime.client import RealtimeClientError
from monkeybot_cli.realtime.wire_encode import encode_client_frame

__all__ = [
    "RealtimeClientError",
    "encode_client_frame",
    "is_exit_command",
]
