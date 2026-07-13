"""Shim — implementation lives in ``monkeybot_cli.realtime.push_to_talk``."""

from __future__ import annotations

from monkeybot_cli.realtime import push_to_talk as _impl
from monkeybot_cli.realtime.push_to_talk import PushToTalkError, PushToTalkGate

# Re-export module attrs so tests can patch ``monkeybot.cli.push_to_talk._HAS_PYNPUT``.
_HAS_PYNPUT = _impl._HAS_PYNPUT
keyboard = _impl.keyboard

__all__ = ["PushToTalkError", "PushToTalkGate", "_HAS_PYNPUT", "keyboard"]
