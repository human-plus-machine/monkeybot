"""Tests for realtime client frame encoding helpers."""

from __future__ import annotations

import json

from monkeybot.cli.realtime_client import encode_client_frame, is_exit_command
from monkeybot.gateway.realtime.wire import (
    ClientCloseFrame,
    ClientInterruptFrame,
    ClientTextFrame,
)


def test_encode_client_text_frame() -> None:
    frame = ClientTextFrame(text="hello")
    encoded = json.loads(encode_client_frame(frame))
    assert encoded == {"kind": "text", "text": "hello"}


def test_encode_client_interrupt_frame() -> None:
    frame = ClientInterruptFrame()
    encoded = json.loads(encode_client_frame(frame))
    assert encoded == {"kind": "interrupt"}


def test_encode_client_audio_stream_end_frame() -> None:
    from monkeybot.gateway.realtime.wire import ClientAudioStreamEndFrame

    frame = ClientAudioStreamEndFrame()
    encoded = json.loads(encode_client_frame(frame))
    assert encoded == {"kind": "audio_stream_end"}


def test_encode_client_close_frame() -> None:
    frame = ClientCloseFrame(reason="done")
    encoded = json.loads(encode_client_frame(frame))
    assert encoded == {"kind": "close", "reason": "done"}


def test_is_exit_command() -> None:
    assert is_exit_command("/bye")
    assert is_exit_command("/BYE")
    assert is_exit_command("  /quit  ")
    assert is_exit_command("/exit")
    assert not is_exit_command("bye")
    assert not is_exit_command("/help")
