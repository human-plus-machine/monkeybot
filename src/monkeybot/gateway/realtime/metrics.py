"""Realtime session and turn metrics."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from monkeybot.core.logging_utils import kv

logger = logging.getLogger("monkeybot.gateway.realtime.metrics")


@dataclass
class RealtimeMetrics:
    """Accumulated metrics for one realtime session."""

    session_id: str
    request_id: str
    opened_at: float = field(default_factory=time.monotonic)
    closed_at: float | None = None
    close_reason: str = "unknown"
    user_audio_sec: float = 0.0
    model_audio_sec: float = 0.0
    interrupt_count: int = 0
    turn_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    turn_latencies_ms: list[float] = field(default_factory=list)
    turn_model_durations_ms: list[float] = field(default_factory=list)
    turn_tool_counts: list[int] = field(default_factory=list)
    _current_turn_started_at: float | None = None
    _current_turn_first_output_at: float | None = None
    _current_turn_tool_count: int = 0

    def mark_user_audio_sec(self, duration: float) -> None:
        self.user_audio_sec += duration

    def mark_model_audio_sec(self, duration: float) -> None:
        self.model_audio_sec += duration

    def mark_interrupt(self) -> None:
        self.interrupt_count += 1

    def mark_usage(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)

    def start_turn(self, now: float | None = None) -> None:
        """Call when a user turn boundary is finalized (model is about to respond)."""
        self._current_turn_started_at = now or time.monotonic()
        self._current_turn_first_output_at = None
        self._current_turn_tool_count = 0

    def mark_first_output(self, now: float | None = None) -> None:
        """Call when the first assistant output delta arrives."""
        if self._current_turn_started_at is None:
            return
        now = now or time.monotonic()
        self._current_turn_first_output_at = now
        self.turn_latencies_ms.append(
            round((now - self._current_turn_started_at) * 1000, 2)
        )

    def mark_tool_in_turn(self) -> None:
        self._current_turn_tool_count += 1

    def end_turn(self, now: float | None = None) -> None:
        """Call when the assistant turn boundary is finalized."""
        self.turn_count += 1
        self.turn_tool_counts.append(self._current_turn_tool_count)
        if self._current_turn_first_output_at is not None:
            now = now or time.monotonic()
            self.turn_model_durations_ms.append(
                round((now - self._current_turn_first_output_at) * 1000, 2)
            )
        self._current_turn_started_at = None
        self._current_turn_first_output_at = None
        self._current_turn_tool_count = 0

    @property
    def current_turn_started_at(self) -> float | None:
        return self._current_turn_started_at

    def close(self, reason: str, now: float | None = None) -> None:
        self.close_reason = reason
        self.closed_at = now or time.monotonic()

    def as_dict(self) -> dict[str, Any]:
        duration = 0.0
        if self.closed_at is not None:
            duration = round(self.closed_at - self.opened_at, 3)
        return {
            "event": "realtime_session_summary",
            "session_id": self.session_id,
            "request_id": self.request_id,
            "realtime_session_duration_sec": duration,
            "realtime_session_user_audio_sec": round(self.user_audio_sec, 3),
            "realtime_session_model_audio_sec": round(self.model_audio_sec, 3),
            "realtime_session_interrupt_count": self.interrupt_count,
            "realtime_session_turn_count": self.turn_count,
            "realtime_session_input_tokens": self.input_tokens,
            "realtime_session_output_tokens": self.output_tokens,
            "realtime_session_close_reason": self.close_reason,
            "realtime_turn_latency_ms_avg": round(sum(self.turn_latencies_ms) / len(self.turn_latencies_ms), 2)
            if self.turn_latencies_ms
            else 0.0,
            "realtime_turn_model_duration_ms_avg": round(
                sum(self.turn_model_durations_ms) / len(self.turn_model_durations_ms), 2
            )
            if self.turn_model_durations_ms
            else 0.0,
            "realtime_turn_tool_count_total": sum(self.turn_tool_counts),
        }

    def emit_summary(self) -> None:
        if self.closed_at is None:
            self.close("unknown")
        logger.info("realtime session summary %s", kv(**self.as_dict()))
