"""WebSocket frame protocol for MonkeyBot realtime sessions.

Frames are either binary audio chunks or JSON control messages. The gateway keeps
client/gateway audio formats fixed per deployment; the provider may negotiate a
different format internally, but the client contract is stable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from monkeybot.core.llm.realtime_provider import AudioFormat


@dataclass(frozen=True)
class ClientConnectFrame:
    """First message from the client; requests a realtime session for the given id."""

    kind: Literal["connect"] = "connect"
    session_id: str = ""


@dataclass(frozen=True)
class ClientTextFrame:
    """Text input from the client."""

    kind: Literal["text"] = "text"
    text: str = ""


@dataclass(frozen=True)
class ClientInterruptFrame:
    """Client explicitly requests to interrupt the current assistant turn."""

    kind: Literal["interrupt"] = "interrupt"


@dataclass(frozen=True)
class ClientAudioStreamEndFrame:
    """Client finished a push-to-talk utterance; provider should treat speech as complete."""

    kind: Literal["audio_stream_end"] = "audio_stream_end"


@dataclass(frozen=True)
class ClientCloseFrame:
    """Client is closing the realtime session gracefully."""

    kind: Literal["close"] = "close"
    reason: str = "client_close"


ClientFrame = (
    ClientConnectFrame
    | ClientTextFrame
    | ClientInterruptFrame
    | ClientAudioStreamEndFrame
    | ClientCloseFrame
)


@dataclass(frozen=True)
class ServerConnectedFrame:
    kind: Literal["connected"] = "connected"
    session_id: str = ""
    input_format: str = "pcm_s16le_24khz_mono"
    output_format: str = "pcm_s16le_24khz_mono"
    chunk_ms: int = 200


@dataclass(frozen=True)
class ServerTextDeltaFrame:
    kind: Literal["text_delta"] = "text_delta"
    delta: str = ""
    is_final: bool = False


@dataclass(frozen=True)
class ServerAudioFrame:
    kind: Literal["audio"] = "audio"
    chunk: bytes = b""


@dataclass(frozen=True)
class ServerInterruptedFrame:
    kind: Literal["interrupted"] = "interrupted"


@dataclass(frozen=True)
class ServerTurnBoundaryFrame:
    kind: Literal["turn_boundary"] = "turn_boundary"
    role: Literal["user", "assistant"] = "user"


@dataclass(frozen=True)
class ServerToolCallFrame:
    kind: Literal["tool_call"] = "tool_call"
    call_id: str = ""
    name: str = ""
    args: dict[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ServerErrorFrame:
    kind: Literal["error"] = "error"
    error: str = ""


@dataclass(frozen=True)
class ServerSessionEndedFrame:
    kind: Literal["session_ended"] = "session_ended"
    reason: str = "session_end"


ServerFrame = (
    ServerConnectedFrame
    | ServerTextDeltaFrame
    | ServerAudioFrame
    | ServerInterruptedFrame
    | ServerTurnBoundaryFrame
    | ServerToolCallFrame
    | ServerErrorFrame
    | ServerSessionEndedFrame
)


def _frame_size_for(fmt: AudioFormat) -> int:
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    samples = int(fmt.sample_rate_hz * fmt.frame_ms / 1000)
    return samples * bytes_per_sample * fmt.channels


def parse_client_frame(data: str | bytes) -> ClientFrame:
    """Parse a client JSON control frame."""
    if isinstance(data, bytes):
        raise ProtocolError("Binary frames are audio chunks and must be handled before parsing")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON frame: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Client frame must be a JSON object")
    kind = str(payload.get("kind") or payload.get("type") or "")
    if kind == "connect":
        return ClientConnectFrame(session_id=str(payload.get("session_id", "")))
    if kind == "text":
        return ClientTextFrame(text=str(payload.get("text", "")))
    if kind == "interrupt":
        return ClientInterruptFrame()
    if kind == "audio_stream_end":
        return ClientAudioStreamEndFrame()
    if kind == "close":
        return ClientCloseFrame(reason=str(payload.get("reason", "client_close")))
    raise ProtocolError(f"Unknown client frame kind: {kind!r}")


def parse_server_frame(data: str) -> ServerFrame:
    """Parse a server JSON control frame."""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON frame: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Server frame must be a JSON object")
    kind = str(payload.get("kind") or payload.get("type") or "")
    if kind == "connected":
        return ServerConnectedFrame(
            session_id=str(payload.get("session_id", "")),
            input_format=str(payload.get("input_format", "pcm_s16le_24khz_mono")),
            output_format=str(payload.get("output_format", "pcm_s16le_24khz_mono")),
            chunk_ms=int(payload.get("chunk_ms", 200)),
        )
    if kind == "text_delta":
        return ServerTextDeltaFrame(
            delta=str(payload.get("delta", "")),
            is_final=bool(payload.get("is_final", False)),
        )
    if kind == "interrupted":
        return ServerInterruptedFrame()
    if kind == "turn_boundary":
        role = str(payload.get("role", "user"))
        return ServerTurnBoundaryFrame(role=role)  # type: ignore[arg-type]
    if kind == "tool_call":
        return ServerToolCallFrame(
            call_id=str(payload.get("call_id", "")),
            name=str(payload.get("name", "")),
            args=dict(payload.get("args") or {}),
        )
    if kind == "error":
        return ServerErrorFrame(error=str(payload.get("error", "")))
    if kind == "session_ended":
        return ServerSessionEndedFrame(reason=str(payload.get("reason", "session_end")))
    raise ProtocolError(f"Unknown server frame kind: {kind!r}")


def encode_server_frame(frame: ServerFrame) -> str | bytes:
    """Encode a server frame to JSON text or binary audio."""
    if isinstance(frame, ServerAudioFrame):
        return frame.chunk
    return json.dumps(
        {
            "kind": frame.kind,
            **(
                {"session_id": frame.session_id, "input_format": frame.input_format,
                 "output_format": frame.output_format, "chunk_ms": frame.chunk_ms}
                if isinstance(frame, ServerConnectedFrame)
                else {}
            ),
            **({"delta": frame.delta, "is_final": frame.is_final} if isinstance(frame, ServerTextDeltaFrame) else {}),
            **({"role": frame.role} if isinstance(frame, ServerTurnBoundaryFrame) else {}),
            **({"call_id": frame.call_id, "name": frame.name, "args": frame.args}
               if isinstance(frame, ServerToolCallFrame) else {}),
            **({"error": frame.error} if isinstance(frame, ServerErrorFrame) else {}),
            **({"reason": frame.reason} if isinstance(frame, ServerSessionEndedFrame) else {}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ProtocolError(Exception):
    """Malformed realtime frame."""


__all__ = [
    "ClientAudioStreamEndFrame",
    "ClientCloseFrame",
    "ClientConnectFrame",
    "ClientFrame",
    "ClientInterruptFrame",
    "ClientTextFrame",
    "ProtocolError",
    "ServerAudioFrame",
    "ServerConnectedFrame",
    "ServerErrorFrame",
    "ServerFrame",
    "ServerInterruptedFrame",
    "ServerSessionEndedFrame",
    "ServerTextDeltaFrame",
    "ServerToolCallFrame",
    "ServerTurnBoundaryFrame",
    "encode_server_frame",
    "parse_client_frame",
    "parse_server_frame",
]
