"""Vendor-agnostic contract for persistent duplex realtime model sessions.

This protocol is parallel to :class:`~monkeybot.core.llm.provider.Provider` but is
not a variant of it. Realtime vendor APIs (Gemini Live, OpenAI Realtime, Bedrock
Nova Sonic) are persistent duplex sessions, not request/response-per-call streams.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias

from monkeybot.core.types.types_tools import ToolDef


@dataclass(frozen=True)
class AudioFormat:
    """Audio wire format for realtime input/output."""

    encoding: str
    sample_rate_hz: int
    channels: int
    frame_ms: int

    def __str__(self) -> str:
        return f"{self.encoding}_{self.sample_rate_hz}hz_{self.channels}ch_{self.frame_ms}ms"


@dataclass(frozen=True)
class RealtimeSessionConfig:
    """Parameters used to open a persistent realtime vendor session."""

    model: str
    system_prompt: str
    tools: Sequence[ToolDef]
    preferred_input_format: AudioFormat = AudioFormat(
        encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
    )
    preferred_output_format: AudioFormat = AudioFormat(
        encoding="pcm_s16le", sample_rate_hz=24000, channels=1, frame_ms=200
    )
    voice: str | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class RealtimePartialTranscript:
    kind: Literal["RealtimePartialTranscript"] = "RealtimePartialTranscript"
    text: str = ""
    is_final: bool = False


@dataclass(frozen=True)
class RealtimeAudioDelta:
    kind: Literal["RealtimeAudioDelta"] = "RealtimeAudioDelta"
    chunk: bytes = b""


@dataclass(frozen=True)
class RealtimeTextDelta:
    """Text delta emitted by the model during a realtime turn."""

    kind: Literal["RealtimeTextDelta"] = "RealtimeTextDelta"
    text: str = ""


@dataclass(frozen=True)
class RealtimeTurnBoundary:
    """Provider signaled the end of the current user or assistant turn."""

    kind: Literal["RealtimeTurnBoundary"] = "RealtimeTurnBoundary"
    role: Literal["user", "assistant"] = "user"


@dataclass(frozen=True)
class RealtimeToolCall:
    """Tool call emitted by a realtime provider at turn boundary."""

    kind: Literal["RealtimeToolCall"] = "RealtimeToolCall"
    call_id: str = ""
    name: str = ""
    args: dict[str, object] = field(default_factory=dict)
    parse_error: str | None = None


@dataclass(frozen=True)
class RealtimeInterrupted:
    """Provider acknowledged that the current turn was interrupted."""

    kind: Literal["RealtimeInterrupted"] = "RealtimeInterrupted"


@dataclass(frozen=True)
class RealtimeUsage:
    kind: Literal["RealtimeUsage"] = "RealtimeUsage"
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class RealtimeError:
    kind: Literal["RealtimeError"] = "RealtimeError"
    error: str = ""


@dataclass(frozen=True)
class RealtimeDone:
    """Provider has finished emitting output for the current turn."""

    kind: Literal["RealtimeDone"] = "RealtimeDone"


RealtimeEvent: TypeAlias = (
    RealtimePartialTranscript
    | RealtimeAudioDelta
    | RealtimeTextDelta
    | RealtimeTurnBoundary
    | RealtimeToolCall
    | RealtimeInterrupted
    | RealtimeUsage
    | RealtimeError
    | RealtimeDone
)


class RealtimeSession(Protocol):
    """One open realtime vendor session."""

    @property
    def input_format(self) -> AudioFormat:
        """Actual negotiated input format (may differ from requested)."""

    @property
    def output_format(self) -> AudioFormat:
        """Actual negotiated output format (may differ from requested)."""

    async def send_audio(self, chunk: bytes) -> None:
        """Send one audio chunk from the user."""

    async def end_audio_turn(self) -> None:
        """Signal that the user finished speaking (end of push-to-talk utterance).

        Providers that use voice-activity detection can treat this as an explicit
        end-of-speech cue so the model responds immediately instead of waiting for
        silence timeout.
        """

    async def send_text(self, text: str) -> None:
        """Send text from the user."""

    async def send_context(self, text: str) -> None:
        """Inject non-user context (e.g., a tool result) into the live session."""

    async def send_tool_results(
        self,
        results: Sequence[tuple[str, str, dict[str, object], bool]],
    ) -> None:
        """Return tool results to the live session.

        Each result is ``(call_id, tool_name, response_payload, is_error)``.
        Providers that require a dedicated tool-response RPC (e.g. Gemini Live
        ``send_tool_response``) must implement that here. Providers without a
        dedicated RPC may fall back to ``send_context``.
        """

    async def interrupt(self) -> None:
        """Tell the provider to cancel the current assistant turn."""

    def events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield provider events for the lifetime of the session."""

    async def close(self, *, reason: str = "session_end") -> None:
        """Close the vendor session gracefully."""


class RealtimeProvider(Protocol):
    """Factory for persistent duplex realtime sessions."""

    @property
    def name(self) -> str:
        """Stable provider id (e.g. ``"gemini-live"``)."""

    async def connect(self, *, config: RealtimeSessionConfig) -> RealtimeSession:
        """Open a new vendor session and return it."""


__all__ = [
    "AudioFormat",
    "RealtimeDone",
    "RealtimeError",
    "RealtimeEvent",
    "RealtimeInterrupted",
    "RealtimePartialTranscript",
    "RealtimeProvider",
    "RealtimeSession",
    "RealtimeSessionConfig",
    "RealtimeToolCall",
    "RealtimeTextDelta",
    "RealtimeTurnBoundary",
    "RealtimeUsage",
    "RealtimeAudioDelta",
]
