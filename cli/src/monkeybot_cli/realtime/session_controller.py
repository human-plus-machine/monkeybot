"""WebSocket session controller that emits ChatUiEvent for the shared TUI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Literal

import websockets
from monkeybot.gateway.realtime.wire import (
    ClientAudioStreamEndFrame,
    ClientCloseFrame,
    ClientElicitationResponseFrame,
    ClientInterruptFrame,
    ClientTextFrame,
    ClientToolConfirmationResponseFrame,
    ProtocolError,
    ServerConnectedFrame,
    ServerElicitationFrame,
    ServerErrorFrame,
    ServerInterruptedFrame,
    ServerSessionEndedFrame,
    ServerTextDeltaFrame,
    ServerToolCallFrame,
    ServerToolConfirmationFrame,
    ServerToolResultFrame,
    ServerTurnBoundaryFrame,
    ServerUsageFrame,
    ServerUserTranscriptFrame,
    parse_server_frame,
)
from websockets.typing import Data

from monkeybot_cli.chat_session import ChatUiEvent, HitlAnswer
from monkeybot_cli.chat_status_bar import UsageStore, parse_usage_response
from monkeybot_cli.realtime.wire_encode import encode_client_frame

logger = logging.getLogger(__name__)

EmitFn = Callable[[ChatUiEvent], None]

_MIC_ENERGY_THRESHOLD_DB = -40.0
_POST_SPEAK_MUTE_SEC = 0.4
_AUDIO_CHUNK_EMIT_EVERY = 5

VoiceState = Literal["listening", "muted", "ptt_held", "speaking"]


class RealtimeSessionController:
    """Owns the realtime WebSocket loop; emits UI events; never writes stdout."""

    turn_based: bool = False

    def __init__(
        self,
        *,
        gateway_url: str,
        session_id: str,
        emit: EmitFn | None = None,
        audio_enabled: bool = False,
        audio_recorder: Any | None = None,
        audio_player: Any | None = None,
        push_to_talk: Any | None = None,
        verbose: bool = False,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.session_id = session_id
        self._emit_fn = emit or (lambda _e: None)
        self.audio_enabled = audio_enabled and audio_recorder is not None
        self.audio_recorder = audio_recorder
        self.audio_player = audio_player
        self.push_to_talk = push_to_talk
        self.verbose = verbose
        self.show_usage = False
        self.usage = UsageStore()
        self.stream_error = False
        self._stream_alive = False
        self._reconnecting = False
        self._closed = False
        self._stop = asyncio.Event()
        self._ws: Any | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._assistant_open = False
        self._model_speaking = False
        self._last_model_audio_at = 0.0
        self._tui_ptt_held = False
        self._voice_state: VoiceState | None = None
        self._pending_hitl: dict[str, Any] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._out_chunk_n = 0
        self._in_chunk_n = 0
        self._last_usage_payload: dict[str, Any] | None = None

    def _emit(self, kind: str, **payload: Any) -> None:
        self._emit_fn(ChatUiEvent(kind=kind, payload=payload))

    def set_emit(self, emit: EmitFn) -> None:
        """Replace the UI event sink (used when the TUI takes ownership)."""
        self._emit_fn = emit

    @property
    def stream_alive(self) -> bool:
        return self._stream_alive and not self._closed

    @property
    def reconnecting(self) -> bool:
        return self._reconnecting

    def set_ptt_held(self, held: bool) -> None:
        """In-TUI Space PTT (SSH-safe alternative to global pynput)."""
        self._tui_ptt_held = bool(held)
        self._emit_voice_state()

    def _ptt_held(self) -> bool:
        if self.push_to_talk is not None and self.push_to_talk.is_held():
            return True
        return self._tui_ptt_held

    def _mic_open(self) -> bool:
        if self._stop.is_set():
            return False
        if self._model_speaking:
            return False
        if (time.monotonic() - self._last_model_audio_at) < _POST_SPEAK_MUTE_SEC:
            return False
        if self.push_to_talk is not None or self._tui_ptt_held:
            return self._ptt_held()
        # No PTT configured: open mic when audio enabled (open-mic mode).
        return self.audio_enabled

    def _compute_voice_state(self, *, level_db: float | None = None) -> VoiceState:
        if self._model_speaking:
            return "speaking"
        if self._ptt_held() and self._mic_open():
            return "ptt_held"
        if self._mic_open():
            return "listening"
        return "muted"

    def _emit_voice_state(self, *, level_db: float | None = None) -> None:
        state = self._compute_voice_state(level_db=level_db)
        if state == self._voice_state and level_db is None:
            return
        self._voice_state = state
        payload: dict[str, Any] = {"state": state}
        if level_db is not None:
            payload["level_db"] = level_db
        self._emit("voice_state", **payload)

    async def connect(self, resume_session_id: str | None = None) -> None:
        if resume_session_id and resume_session_id.strip():
            self.session_id = resume_session_id.strip()
        url = f"{self.gateway_url}/sessions/{self.session_id}/realtime"
        self._stop.clear()
        self._closed = False
        if self.push_to_talk is not None:
            try:
                self.push_to_talk.start()
            except Exception:
                logger.exception("push-to-talk start failed")
                self._emit(
                    "device_error",
                    message="Push-to-talk failed to start",
                    hint="Check Accessibility permissions, or use in-TUI Space PTT / --text.",
                )
                self.push_to_talk = None
        try:
            self._ws = await websockets.connect(url)
            await self._ws.send(
                json.dumps({"kind": "connect", "session_id": self.session_id})
            )
        except websockets.exceptions.InvalidStatus as exc:
            status = getattr(exc, "status_code", getattr(exc, "status", "unknown"))
            raise RuntimeError(f"Failed to connect to {url}: HTTP {status}") from exc
        except Exception as exc:
            raise RuntimeError(f"Realtime connect failed: {exc}") from exc

        self._stream_alive = True
        self.stream_error = False
        self._recv_task = asyncio.create_task(self._receive_loop(), name="rt-recv")
        if self.audio_enabled and self.audio_recorder is not None:
            self._audio_task = asyncio.create_task(self._send_audio(), name="rt-audio")
        self._emit("connection_state", state="connected")
        self._emit_voice_state()

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._stop.is_set():
                    return
                await self._handle_server_frame(raw)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("realtime websocket closed")
        except Exception as exc:
            logger.exception("realtime receive loop failed")
            self.stream_error = True
            self._emit("stream_failed", message=str(exc), fatal=True)
        finally:
            self._stream_alive = False
            if not self._stop.is_set():
                self._emit("stream_ended")

    async def _send_audio(self) -> None:
        if self.audio_recorder is None or self._ws is None:
            return
        was_open = False
        while not self._stop.is_set():
            try:
                chunk = await asyncio.to_thread(self.audio_recorder.read_chunk)
            except Exception as exc:
                self._emit(
                    "device_error",
                    message=f"Microphone read failed: {exc}",
                    hint="Check PortAudio / mic permissions, or use --text.",
                )
                return
            if self._stop.is_set() or self._ws is None:
                return
            mic_open = self._mic_open()
            if was_open and not mic_open:
                try:
                    await self._ws.send(encode_client_frame(ClientAudioStreamEndFrame()))
                except Exception:
                    logger.warning("failed to send audio_stream_end", exc_info=True)
                self._emit_voice_state()
            was_open = mic_open
            if not mic_open:
                self._emit_voice_state()
                await asyncio.sleep(0.02)
                continue
            level: float | None = None
            if hasattr(self.audio_recorder, "chunk_peak_db"):
                level = float(self.audio_recorder.chunk_peak_db(chunk))
                if level < _MIC_ENERGY_THRESHOLD_DB:
                    await asyncio.sleep(0)
                    continue
            try:
                await self._ws.send(chunk)
            except Exception as exc:
                logger.exception("realtime outbound audio send failed")
                self.stream_error = True
                self._emit("stream_failed", message=str(exc), fatal=True)
                return
            self._in_chunk_n += 1
            if self._in_chunk_n % _AUDIO_CHUNK_EMIT_EVERY == 0:
                self._emit("audio_chunk", direction="in", level_db=level)
                self._emit_voice_state(level_db=level)
            else:
                self._emit_voice_state(level_db=level)
            await asyncio.sleep(0)

    async def _handle_server_frame(self, raw: Data) -> None:
        if isinstance(raw, bytes):
            await self._on_audio_bytes(raw)
            return
        try:
            frame = parse_server_frame(raw)
        except ProtocolError as exc:
            logger.warning("ignored malformed server frame: %s", exc)
            return

        if isinstance(frame, ServerConnectedFrame):
            self._on_connected(frame)
        elif isinstance(frame, ServerUserTranscriptFrame):
            self._emit(
                "user_transcript",
                text=frame.text,
                is_final=bool(frame.is_final),
            )
        elif isinstance(frame, ServerTextDeltaFrame):
            self._on_text_delta(frame)
        elif isinstance(frame, ServerTurnBoundaryFrame):
            self._on_turn_boundary(frame)
        elif isinstance(frame, ServerToolCallFrame):
            self._emit(
                "tool_started",
                tool=frame.name,
                label=frame.name,
                args=dict(frame.args or {}),
                call_id=frame.call_id or "",
            )
        elif isinstance(frame, ServerToolResultFrame):
            self._emit(
                "tool_finished",
                tool=frame.name,
                error=frame.error,
                result=frame.result or "",
                verbose=self.verbose,
                call_id=frame.call_id or "",
            )
        elif isinstance(frame, ServerUsageFrame):
            payload = dict(frame.usage or {})
            self._last_usage_payload = payload
            view = parse_usage_response(payload)
            self.usage.update(view)
            self._emit("usage_updated", usage=view)
        elif isinstance(frame, ServerToolConfirmationFrame):
            self._pending_hitl = {
                "hitl_kind": "confirm",
                "tool_call_id": frame.tool_call_id,
                "tool_name": frame.tool_name,
                "prompt": frame.prompt,
                "arguments": dict(frame.arguments or {}),
                "timeout_sec": frame.timeout_sec,
            }
            self._emit("hitl_required", **self._pending_hitl)
        elif isinstance(frame, ServerElicitationFrame):
            self._pending_hitl = {
                "hitl_kind": "elicit",
                "elicitation_id": frame.elicitation_id,
                "prompt": frame.prompt,
                "schema": frame.schema,
                "timeout_sec": frame.timeout_sec,
            }
            self._emit("hitl_required", **self._pending_hitl)
        elif isinstance(frame, ServerInterruptedFrame):
            self._model_speaking = False
            self._assistant_open = False
            self._emit_voice_state()
            self._emit("turn_aborted", cancel_ok=True)
        elif isinstance(frame, ServerErrorFrame):
            self._emit("turn_error", error=frame.error)
        elif isinstance(frame, ServerSessionEndedFrame):
            self._emit("stream_ended")
            self._stop.set()
            self._stream_alive = False

    async def _on_audio_bytes(self, raw: bytes) -> None:
        self._model_speaking = True
        self._last_model_audio_at = time.monotonic()
        self._out_chunk_n += 1
        if self._out_chunk_n % _AUDIO_CHUNK_EMIT_EVERY == 0:
            self._emit("audio_chunk", direction="out")
        self._emit_voice_state()
        if self.audio_player is not None:
            await asyncio.to_thread(self.audio_player.write, raw)

    def _on_connected(self, frame: ServerConnectedFrame) -> None:
        self.session_id = frame.session_id or self.session_id
        self._emit(
            "session_ready",
            session_id=self.session_id,
            input_format=frame.input_format,
            output_format=frame.output_format,
            chunk_ms=frame.chunk_ms,
        )

    def _on_text_delta(self, frame: ServerTextDeltaFrame) -> None:
        if not self._assistant_open:
            self._emit("assistant_start")
            self._assistant_open = True
        if frame.delta:
            self._emit("assistant_delta", delta=frame.delta)
        if frame.is_final:
            self._assistant_open = False
            self._emit("turn_complete", usage=None)

    def _on_turn_boundary(self, frame: ServerTurnBoundaryFrame) -> None:
        if frame.role == "assistant":
            self._model_speaking = False
            self._last_model_audio_at = time.monotonic()
            self._emit_voice_state()
            if self._assistant_open:
                self._assistant_open = False
                self._emit("turn_complete", usage=None)
        elif frame.role == "user":
            self._emit("thinking", text="listening…")

    async def submit(self, message: str) -> None:
        if self._ws is None or not self._stream_alive:
            self._emit("error", message="Not connected")
            return
        text = message.strip()
        if not text:
            return
        self._emit("turn_started", request_id="")
        self._emit("thinking", text="thinking…")
        try:
            await self._ws.send(encode_client_frame(ClientTextFrame(text=text)))
        except Exception as exc:
            self._emit("error", message=f"Send failed: {exc}")

    def _spawn_background(self, coro: Any, *, name: str) -> None:
        """Schedule ``coro`` and retain the task so it is not GC'd mid-flight."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("%s called with no running event loop", name)
            return
        task = loop.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def abort_turn(self) -> None:
        if self._ws is None:
            return

        async def _interrupt() -> None:
            try:
                assert self._ws is not None
                await self._ws.send(encode_client_frame(ClientInterruptFrame()))
            except Exception:
                logger.warning("failed to send interrupt frame", exc_info=True)

        self._spawn_background(_interrupt(), name="rt-abort")

    def provide_hitl_answer(self, answer: HitlAnswer) -> None:
        if self._ws is None or self._pending_hitl is None:
            return
        pending = self._pending_hitl
        self._pending_hitl = None

        async def _send() -> None:
            assert self._ws is not None
            if answer.cancelled:
                if pending.get("hitl_kind") == "confirm":
                    frame = ClientToolConfirmationResponseFrame(
                        tool_call_id=str(pending.get("tool_call_id") or ""),
                        approved=False,
                        reason="cancelled",
                    )
                else:
                    frame = ClientElicitationResponseFrame(
                        elicitation_id=str(pending.get("elicitation_id") or ""),
                        user_data=None,
                        cancelled=True,
                    )
            elif pending.get("hitl_kind") == "confirm":
                frame = ClientToolConfirmationResponseFrame(
                    tool_call_id=str(pending.get("tool_call_id") or ""),
                    approved=bool(answer.approved),
                    reason=answer.text or "",
                )
            else:
                frame = ClientElicitationResponseFrame(
                    elicitation_id=str(pending.get("elicitation_id") or ""),
                    user_data=answer.user_data if answer.user_data is not None else answer.text,
                    cancelled=False,
                )
            try:
                await self._ws.send(encode_client_frame(frame))
            except Exception:
                logger.warning("failed to send HITL response frame", exc_info=True)
                self._emit("error", message="Failed to send confirmation response")

        self._spawn_background(_send(), name="rt-hitl")

    async def restart_session(self) -> None:
        self._emit("error", message="Session restart is not supported in realtime mode")

    async def resume_session(self, session_id: str) -> None:
        await self.close()
        self.session_id = session_id
        await self.connect(session_id)

    async def refresh_usage(self) -> None:
        if self._last_usage_payload is not None:
            view = parse_usage_response(self._last_usage_payload)
            self.usage.update(view)
            self._emit("usage_updated", usage=view)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self.push_to_talk is not None:
            try:
                self.push_to_talk.stop()
            except Exception:
                logger.warning("push-to-talk stop failed", exc_info=True)
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.send(encode_client_frame(ClientCloseFrame(reason="client_close")))
            except Exception:
                logger.debug("close frame send failed during shutdown", exc_info=True)
            try:
                await ws.close()
            except Exception:
                logger.debug("websocket close failed during shutdown", exc_info=True)
        for task in (self._audio_task, self._recv_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for task in list(self._background_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background_tasks.clear()
        self._audio_task = None
        self._recv_task = None
        self._stream_alive = False
