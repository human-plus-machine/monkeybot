"""Shared chat renderer contract and session controller protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from monkeybot_cli.chat_session import ChatUiEvent, HitlAnswer
    from monkeybot_cli.chat_status_bar import UsageStore

# Every kind emitted by session controllers (and expected by renderers).
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "usage_updated",
        "session_ready",
        "connection_state",
        "transcript_backfill",
        "thinking",
        "thinking_clear",
        "turn_started",
        "assistant_start",
        "assistant_delta",
        "tool_started",
        "tool_finished",
        "summarizing",
        "summarized",
        "grounding",
        "turn_error",
        "turn_complete",
        "turn_aborted",
        "hitl_required",
        "hitl_failed",
        "hitl_frontend_unsupported",
        "session_busy",
        "error",
        "stream_failed",
        "stream_ended",
        "thinking_trace",
        "thinking_block_delta",
        "thinking_block_complete",
        "voice_state",
        "audio_chunk",
        "device_error",
        "user_transcript",
    }
)


@runtime_checkable
class SessionController(Protocol):
    """Minimal surface the TUI / plain renderer need from a session backend."""

    turn_based: bool
    stream_alive: bool
    stream_error: bool
    reconnecting: bool
    show_usage: bool
    usage: UsageStore
    session_id: str | None

    async def connect(self, resume_session_id: str | None = None) -> None: ...

    async def submit(self, message: str) -> None: ...

    def abort_turn(self) -> None: ...

    def provide_hitl_answer(self, answer: HitlAnswer) -> None: ...

    def set_emit(self, emit: Any) -> None: ...

    async def close(self) -> None: ...

    async def restart_session(self) -> None: ...

    async def resume_session(self, session_id: str) -> None: ...

    async def refresh_usage(self) -> None: ...


@runtime_checkable
class ChatRenderer(Protocol):
    """Sink for ``ChatUiEvent``s from a session controller."""

    def on_event(self, event: ChatUiEvent, controller: SessionController) -> None: ...


__all__ = ["EVENT_KINDS", "ChatRenderer", "SessionController"]
