"""Tests for the realtime WebSocket frame protocol."""

from __future__ import annotations

import json

import pytest

from monkeybot.core.llm.realtime_provider import AudioFormat
from monkeybot.gateway.realtime.wire import (
    ClientCloseFrame,
    ClientConnectFrame,
    ClientInterruptFrame,
    ClientTextFrame,
    ProtocolError,
    ServerAudioFrame,
    ServerConnectedFrame,
    ServerErrorFrame,
    ServerInterruptedFrame,
    ServerSessionEndedFrame,
    ServerTextDeltaFrame,
    ServerToolCallFrame,
    ServerTurnBoundaryFrame,
    audio_duration_sec,
    encode_server_frame,
    parse_client_frame,
    parse_server_frame,
)


def test_parse_connect_frame() -> None:
    frame = parse_client_frame(json.dumps({"kind": "connect", "session_id": "abc123"}))
    assert isinstance(frame, ClientConnectFrame)
    assert frame.session_id == "abc123"


def test_parse_text_frame() -> None:
    frame = parse_client_frame(json.dumps({"kind": "text", "text": "hello"}))
    assert isinstance(frame, ClientTextFrame)
    assert frame.text == "hello"


def test_parse_interrupt_frame() -> None:
    frame = parse_client_frame(json.dumps({"kind": "interrupt"}))
    assert isinstance(frame, ClientInterruptFrame)


def test_parse_audio_stream_end_frame() -> None:
    from monkeybot.gateway.realtime.wire import ClientAudioStreamEndFrame

    frame = parse_client_frame(json.dumps({"kind": "audio_stream_end"}))
    assert isinstance(frame, ClientAudioStreamEndFrame)


def test_parse_close_frame() -> None:
    frame = parse_client_frame(json.dumps({"kind": "close", "reason": "done"}))
    assert isinstance(frame, ClientCloseFrame)
    assert frame.reason == "done"


def test_parse_binary_raises() -> None:
    """Binary frames are audio chunks and must be handled by the caller, not parsed."""
    with pytest.raises(ProtocolError, match="Binary frames"):
        parse_client_frame(b"\x00\x01\x02")


def test_parse_unknown_kind_raises() -> None:
    with pytest.raises(ProtocolError, match="Unknown client frame kind"):
        parse_client_frame(json.dumps({"kind": "unknown"}))


def test_parse_invalid_json_raises() -> None:
    with pytest.raises(ProtocolError, match="Invalid JSON"):
        parse_client_frame("not json")


def test_encode_connected_frame() -> None:
    frame = ServerConnectedFrame(session_id="s1", input_format="pcm", output_format="pcm", chunk_ms=80)
    encoded = json.loads(encode_server_frame(frame))
    assert encoded["kind"] == "connected"
    assert encoded["session_id"] == "s1"
    assert encoded["chunk_ms"] == 80


def test_encode_text_delta_frame() -> None:
    frame = ServerTextDeltaFrame(delta="hi", is_final=True)
    encoded = json.loads(encode_server_frame(frame))
    assert encoded["kind"] == "text_delta"
    assert encoded["delta"] == "hi"
    assert encoded["is_final"] is True


def test_encode_audio_frame_returns_bytes() -> None:
    frame = ServerAudioFrame(chunk=b"audio data")
    encoded = encode_server_frame(frame)
    assert isinstance(encoded, bytes)
    assert encoded == b"audio data"


def test_encode_tool_call_frame() -> None:
    frame = ServerToolCallFrame(call_id="c1", name="search", args={"q": "x"})
    encoded = json.loads(encode_server_frame(frame))
    assert encoded["kind"] == "tool_call"
    assert encoded["call_id"] == "c1"
    assert encoded["args"] == {"q": "x"}


def test_encode_turn_boundary_frame() -> None:
    frame = ServerTurnBoundaryFrame(role="assistant")
    encoded = json.loads(encode_server_frame(frame))
    assert encoded["kind"] == "turn_boundary"
    assert encoded["role"] == "assistant"


def test_encode_error_and_session_ended_frames() -> None:
    assert json.loads(encode_server_frame(ServerErrorFrame(error="boom"))) == {
        "kind": "error",
        "error": "boom",
    }
    assert json.loads(encode_server_frame(ServerSessionEndedFrame(reason="timeout"))) == {
        "kind": "session_ended",
        "reason": "timeout",
    }
    assert json.loads(encode_server_frame(ServerInterruptedFrame())) == {
        "kind": "interrupted",
    }


def test_parse_server_connected_frame() -> None:
    frame = parse_server_frame(
        json.dumps(
            {
                "kind": "connected",
                "session_id": "s1",
                "input_format": "pcm",
                "output_format": "pcm",
                "chunk_ms": 80,
            }
        )
    )
    assert isinstance(frame, ServerConnectedFrame)
    assert frame.session_id == "s1"
    assert frame.chunk_ms == 80


def test_parse_server_text_delta_frame() -> None:
    frame = parse_server_frame(json.dumps({"kind": "text_delta", "delta": "hi", "is_final": True}))
    assert isinstance(frame, ServerTextDeltaFrame)
    assert frame.delta == "hi"
    assert frame.is_final is True


def test_parse_server_turn_boundary_frame() -> None:
    frame = parse_server_frame(json.dumps({"kind": "turn_boundary", "role": "assistant"}))
    assert isinstance(frame, ServerTurnBoundaryFrame)
    assert frame.role == "assistant"


def test_parse_server_tool_call_frame() -> None:
    frame = parse_server_frame(
        json.dumps({"kind": "tool_call", "call_id": "c1", "name": "search", "args": {"q": "x"}})
    )
    assert isinstance(frame, ServerToolCallFrame)
    assert frame.name == "search"
    assert frame.args == {"q": "x"}


def test_parse_server_error_and_session_ended_frames() -> None:
    assert parse_server_frame(json.dumps({"kind": "error", "error": "boom"})) == ServerErrorFrame(
        error="boom"
    )
    assert parse_server_frame(
        json.dumps({"kind": "session_ended", "reason": "timeout"})
    ) == ServerSessionEndedFrame(reason="timeout")
    assert isinstance(parse_server_frame(json.dumps({"kind": "interrupted"})), ServerInterruptedFrame)


def test_parse_server_unknown_kind_raises() -> None:
    with pytest.raises(ProtocolError, match="Unknown server frame kind"):
        parse_server_frame(json.dumps({"kind": "unknown"}))


def test_audio_duration_sec() -> None:
    fmt = AudioFormat(encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200)
    # 24000 samples/sec * 2 bytes = 48000 bytes/sec; 4800 bytes => 0.1s
    assert audio_duration_sec(b"\x00" * 4800, fmt) == pytest.approx(0.1)
