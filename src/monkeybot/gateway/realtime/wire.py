"""WebSocket frame protocol for MonkeyBot realtime sessions.

Frames are either binary audio chunks or JSON control messages. The gateway keeps
client/gateway audio formats fixed per deployment; the provider may negotiate a
different format internally, but the client contract is stable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ClientToolConfirmationResponseFrame:
    kind: Literal["tool_confirmation_response"] = "tool_confirmation_response"
    tool_call_id: str = ""
    approved: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ClientElicitationResponseFrame:
    kind: Literal["elicitation_response"] = "elicitation_response"
    elicitation_id: str = ""
    user_data: Any = None
    cancelled: bool = False


ClientFrame = (
    ClientConnectFrame
    | ClientTextFrame
    | ClientInterruptFrame
    | ClientAudioStreamEndFrame
    | ClientCloseFrame
    | ClientToolConfirmationResponseFrame
    | ClientElicitationResponseFrame
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
class ServerToolResultFrame:
    kind: Literal["tool_result"] = "tool_result"
    call_id: str = ""
    name: str = ""
    result: str = ""
    error: str | None = None


@dataclass(frozen=True)
class ServerUserTranscriptFrame:
    kind: Literal["user_transcript"] = "user_transcript"
    text: str = ""
    is_final: bool = True


@dataclass(frozen=True)
class ServerUsageFrame:
    kind: Literal["usage"] = "usage"
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerToolConfirmationFrame:
    kind: Literal["tool_confirmation"] = "tool_confirmation"
    tool_call_id: str = ""
    tool_name: str = ""
    prompt: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_sec: float | None = None


@dataclass(frozen=True)
class ServerElicitationFrame:
    kind: Literal["elicitation"] = "elicitation"
    elicitation_id: str = ""
    prompt: str = ""
    schema: dict[str, Any] | None = None
    timeout_sec: float | None = None


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
    | ServerToolResultFrame
    | ServerUserTranscriptFrame
    | ServerUsageFrame
    | ServerToolConfirmationFrame
    | ServerElicitationFrame
    | ServerErrorFrame
    | ServerSessionEndedFrame
)


def _frame_size_for(fmt: AudioFormat) -> int:
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    samples = int(fmt.sample_rate_hz * fmt.frame_ms / 1000)
    return samples * bytes_per_sample * fmt.channels


def audio_duration_sec(chunk: bytes, fmt: AudioFormat) -> float:
    """Duration in seconds for a PCM (or similar) audio chunk."""
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    frame_size = bytes_per_sample * fmt.channels
    if frame_size == 0:
        return 0.0
    samples = len(chunk) // frame_size
    return samples / fmt.sample_rate_hz


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
    if kind == "tool_confirmation_response":
        return ClientToolConfirmationResponseFrame(
            tool_call_id=str(payload.get("tool_call_id", "")),
            approved=bool(payload.get("approved", False)),
            reason=str(payload.get("reason", "")),
        )
    if kind == "elicitation_response":
        return ClientElicitationResponseFrame(
            elicitation_id=str(payload.get("elicitation_id", "")),
            user_data=payload.get("user_data"),
            cancelled=bool(payload.get("cancelled", False)),
        )
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
    if kind == "tool_result":
        err = payload.get("error")
        return ServerToolResultFrame(
            call_id=str(payload.get("call_id", "")),
            name=str(payload.get("name") or payload.get("tool") or ""),
            result=str(payload.get("result", "")),
            error=None if err is None else str(err),
        )
    if kind == "user_transcript":
        return ServerUserTranscriptFrame(
            text=str(payload.get("text", "")),
            is_final=bool(payload.get("is_final", True)),
        )
    if kind == "usage":
        usage = payload.get("usage")
        return ServerUsageFrame(usage=dict(usage) if isinstance(usage, dict) else dict(payload))
    if kind == "tool_confirmation":
        args = payload.get("arguments") or payload.get("args") or {}
        timeout = payload.get("timeout_sec")
        return ServerToolConfirmationFrame(
            tool_call_id=str(payload.get("tool_call_id", "")),
            tool_name=str(payload.get("tool_name") or payload.get("tool") or ""),
            prompt=str(payload.get("prompt") or payload.get("message") or ""),
            arguments=dict(args) if isinstance(args, dict) else {},
            timeout_sec=float(timeout) if timeout is not None else None,
        )
    if kind == "elicitation":
        schema = payload.get("schema") or payload.get("requestedSchema")
        timeout = payload.get("timeout_sec")
        return ServerElicitationFrame(
            elicitation_id=str(payload.get("elicitation_id", "")),
            prompt=str(payload.get("prompt") or payload.get("message") or ""),
            schema=dict(schema) if isinstance(schema, dict) else None,
            timeout_sec=float(timeout) if timeout is not None else None,
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
    payload: dict[str, Any] = {"kind": frame.kind}
    if isinstance(frame, ServerConnectedFrame):
        payload.update(
            {
                "session_id": frame.session_id,
                "input_format": frame.input_format,
                "output_format": frame.output_format,
                "chunk_ms": frame.chunk_ms,
            }
        )
    elif isinstance(frame, ServerTextDeltaFrame):
        payload.update({"delta": frame.delta, "is_final": frame.is_final})
    elif isinstance(frame, ServerTurnBoundaryFrame):
        payload["role"] = frame.role
    elif isinstance(frame, ServerToolCallFrame):
        payload.update(
            {"call_id": frame.call_id, "name": frame.name, "args": frame.args or {}}
        )
    elif isinstance(frame, ServerToolResultFrame):
        payload.update(
            {
                "call_id": frame.call_id,
                "name": frame.name,
                "result": frame.result,
                "error": frame.error,
            }
        )
    elif isinstance(frame, ServerUserTranscriptFrame):
        payload.update({"text": frame.text, "is_final": frame.is_final})
    elif isinstance(frame, ServerUsageFrame):
        payload["usage"] = frame.usage
    elif isinstance(frame, ServerToolConfirmationFrame):
        payload.update(
            {
                "tool_call_id": frame.tool_call_id,
                "tool_name": frame.tool_name,
                "prompt": frame.prompt,
                "arguments": frame.arguments,
                "timeout_sec": frame.timeout_sec,
            }
        )
    elif isinstance(frame, ServerElicitationFrame):
        payload.update(
            {
                "elicitation_id": frame.elicitation_id,
                "prompt": frame.prompt,
                "schema": frame.schema,
                "timeout_sec": frame.timeout_sec,
            }
        )
    elif isinstance(frame, ServerErrorFrame):
        payload["error"] = frame.error
    elif isinstance(frame, ServerSessionEndedFrame):
        payload["reason"] = frame.reason
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ProtocolError(Exception):
    """Malformed realtime frame."""


__all__ = [
    "ClientAudioStreamEndFrame",
    "ClientCloseFrame",
    "ClientConnectFrame",
    "ClientElicitationResponseFrame",
    "ClientFrame",
    "ClientInterruptFrame",
    "ClientTextFrame",
    "ClientToolConfirmationResponseFrame",
    "ProtocolError",
    "ServerAudioFrame",
    "ServerConnectedFrame",
    "ServerElicitationFrame",
    "ServerErrorFrame",
    "ServerFrame",
    "ServerInterruptedFrame",
    "ServerSessionEndedFrame",
    "ServerTextDeltaFrame",
    "ServerToolCallFrame",
    "ServerToolConfirmationFrame",
    "ServerToolResultFrame",
    "ServerTurnBoundaryFrame",
    "ServerUsageFrame",
    "ServerUserTranscriptFrame",
    "audio_duration_sec",
    "encode_server_frame",
    "parse_client_frame",
    "parse_server_frame",
]
