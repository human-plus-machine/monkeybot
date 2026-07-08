"""Per-connection realtime session state machine."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from monkeybot.core.llm.realtime_provider import (
    AudioFormat,
    RealtimeAudioDelta,
    RealtimeDone,
    RealtimeEvent,
    RealtimeInterrupted,
    RealtimePartialTranscript,
    RealtimeProvider,
    RealtimeSession,
    RealtimeTextDelta,
    RealtimeToolCall,
    RealtimeTurnBoundary,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.utterance_buffer import UtteranceBuffer

from .metrics import RealtimeMetrics

logger = logging.getLogger("monkeybot.gateway.realtime.session")


class SessionStateError(Exception):
    """Invalid state transition or operation on a realtime session."""


RealtimeSessionState = Literal["listening", "thinking", "speaking", "interrupted", "tool_running", "closing"]


@dataclass
class RealtimeConnectionState:
    """Mutable state for one realtime WebSocket connection."""

    session_id: str
    request_id: str
    provider: RealtimeProvider
    realtime_session: RealtimeSession
    buffer: UtteranceBuffer = field(default_factory=UtteranceBuffer)
    state: RealtimeSessionState = "listening"
    opened_at: float = 0.0
    last_activity_at: float = 0.0
    tool_results_pending: list[Any] = field(default_factory=list)
    # Queue for subagent/tool results that arrived while the session is busy.
    # The gateway injects them via send_context() when the session reaches idle.
    idle_delivery_queue: asyncio.Queue[list[Any]] = field(default_factory=asyncio.Queue)
    metrics: RealtimeMetrics = field(init=False)
    _closed: bool = False

    def __post_init__(self) -> None:
        self.metrics = RealtimeMetrics(
            session_id=self.session_id,
            request_id=self.request_id,
            opened_at=self.opened_at,
        )

    def transition(self, new_state: RealtimeSessionState) -> None:
        """Validate and apply a state transition."""
        valid = (self.state, new_state) in _VALID_TRANSITIONS
        if not valid:
            raise SessionStateError(
                f"Invalid realtime transition {self.state} -> {new_state}"
            )
        old = self.state
        self.state = new_state
        logger.debug(
            "realtime state transition %s",
            kv(
                request_id=self.request_id,
                session_id=self.session_id,
                old=old,
                new=new_state,
            ),
        )

    def mark_activity(self, now: float) -> None:
        self.last_activity_at = now

    def apply_provider_event(self, event: RealtimeEvent) -> None:
        """Update state, buffer, and metrics based on a provider event."""
        if self.state == "closing":
            return
        if isinstance(event, RealtimePartialTranscript):
            if not event.is_final:
                self.buffer.add_user_text(event.text)
            # Final transcripts are reflected by the turn boundary.
        elif isinstance(event, (RealtimeAudioDelta, RealtimeTextDelta)):
            if isinstance(event, RealtimeAudioDelta):
                self.metrics.mark_model_audio_sec(
                    _audio_duration_sec(event.chunk, self.realtime_session.output_format)
                )
            else:
                self.buffer.add_assistant_text(event.text)
            self.metrics.mark_first_output()
            if self.state in ("listening", "thinking"):
                self.transition("speaking")
        elif isinstance(event, RealtimeTurnBoundary):
            if event.role == "user":
                if self.state in ("speaking", "thinking"):
                    # User interrupted the assistant and then finalized.
                    self.transition("interrupted")
                    self.transition("listening")
                self.buffer.mark_user_turn_boundary()
                self.metrics.start_turn()
            else:
                # Assistant turn boundary.
                self.buffer.mark_assistant_turn_boundary()
                self.metrics.end_turn()
                # mark_assistant_turn_boundary clears the in-flight buffer; check the
                # finalized turn for tool calls.
                if self.buffer.current_assistant_turn().tool_calls:
                    self.transition("tool_running")
                    return
                self.transition("listening")
        elif isinstance(event, RealtimeToolCall):
            self.buffer.add_assistant_tool_call(event)
            self.metrics.mark_tool_in_turn()
        elif isinstance(event, RealtimeInterrupted):
            # Provider acknowledged interrupt.
            if self.state in ("speaking", "thinking"):
                self.transition("interrupted")
        elif isinstance(event, RealtimeDone) and self.state in (
            "speaking",
            "thinking",
            "interrupted",
        ):
            # Assistant turn ended naturally.
            self.buffer.mark_assistant_turn_boundary()
            self.metrics.end_turn()
            self.transition("listening")

    def handle_user_interrupt(self) -> None:
        """Client or user barge-in."""
        if self.state in ("speaking", "thinking"):
            self.metrics.mark_interrupt()
            self.buffer.interrupt()
            self.transition("interrupted")
            # The provider interrupt call is made by the gateway; here we just update state.
        elif self.state == "tool_running":
            # Constraint 4: do not interrupt a tool call once dispatched.
            pass

    def mark_user_audio(self, duration_sec: float) -> None:
        """Track user audio duration and activity."""
        self.metrics.mark_user_audio_sec(duration_sec)

    def is_idle(self) -> bool:
        return self.state == "listening" and not self.buffer.in_user_turn

    def close(self) -> None:
        self._closed = True
        self.state = "closing"


_VALID_TRANSITIONS: set[tuple[RealtimeSessionState, RealtimeSessionState]] = {
    ("listening", "listening"),
    ("listening", "thinking"),
    ("listening", "speaking"),  # model can start speaking immediately after user input
    ("thinking", "speaking"),
    ("thinking", "listening"),
    ("thinking", "interrupted"),
    ("speaking", "interrupted"),
    ("interrupted", "listening"),
    ("speaking", "listening"),
    ("speaking", "tool_running"),
    ("thinking", "tool_running"),
    ("listening", "tool_running"),  # tool-only turns may skip speaking
    ("tool_running", "listening"),
    ("tool_running", "thinking"),
    # ("tool_running", "interrupted") is explicitly disallowed.
}


# Any source state may transition to closing.
for _src in ("listening", "thinking", "speaking", "interrupted", "tool_running"):
    _VALID_TRANSITIONS.add((_src, "closing"))


def _audio_duration_sec(chunk: bytes, fmt: AudioFormat) -> float:
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    frame_size = bytes_per_sample * fmt.channels
    if frame_size == 0:
        return 0.0
    samples = len(chunk) // frame_size
    return samples / fmt.sample_rate_hz
