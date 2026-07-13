"""Shim — implementation lives in ``monkeybot_cli.realtime.audio_io``."""

from __future__ import annotations

from monkeybot_cli.realtime.audio_io import *  # noqa: F403
from monkeybot_cli.realtime.audio_io import AudioIOError, AudioPlayer, AudioRecorder

__all__ = ["AudioIOError", "AudioPlayer", "AudioRecorder"]
