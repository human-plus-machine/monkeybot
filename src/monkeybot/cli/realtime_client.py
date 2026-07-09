"""Realtime WebSocket client for the MonkeyBot CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from typing import Any

import websockets
from websockets.typing import Data

from monkeybot.gateway.realtime.wire import (
    ClientAudioStreamEndFrame,
    ClientCloseFrame,
    ClientInterruptFrame,
    ClientTextFrame,
    ProtocolError,
    ServerConnectedFrame,
    ServerErrorFrame,
    ServerInterruptedFrame,
    ServerSessionEndedFrame,
    ServerTextDeltaFrame,
    ServerToolCallFrame,
    ServerTurnBoundaryFrame,
    parse_server_frame,
)

logger = logging.getLogger(__name__)

# Ignore mic chunks quieter than this (dBFS) so ambient noise does not barge in.
_MIC_ENERGY_THRESHOLD_DB = -40.0
# Keep mic muted briefly after the last model audio chunk so speaker echo dies out.
_POST_SPEAK_MUTE_SEC = 0.4
# Match monkeybot chat exit commands.
_EXIT_COMMANDS = frozenset({"/bye", "/quit", "/exit"})


class RealtimeClientError(Exception):
    """CLI realtime client error."""


def _is_exit_command(line: str) -> bool:
    """Return True for /bye, /quit, /exit (with optional trailing punctuation)."""
    token = line.strip().lower().split(maxsplit=1)[0] if line.strip() else ""
    # Strip trailing punctuation so "/bye." still exits.
    while token and token[-1] in ".,!;:":
        token = token[:-1]
    return token in _EXIT_COMMANDS


class RealtimeCLIClient:
    """Connects to the MonkeyBot realtime WebSocket and drives text/audio I/O."""

    def __init__(
        self,
        gateway_url: str,
        session_id: str,
        *,
        audio_enabled: bool = False,
        audio_recorder: Any | None = None,
        audio_player: Any | None = None,
        push_to_talk: Any | None = None,
        verbose: bool = False,
    ) -> None:
        self.gateway_url = gateway_url
        self.session_id = session_id
        self.audio_enabled = audio_enabled
        self.audio_recorder = audio_recorder
        self.audio_player = audio_player
        self.push_to_talk = push_to_talk
        self.verbose = verbose
        # Half-duplex: mute mic while the model is speaking (and briefly after).
        self._model_speaking = False
        self._last_model_audio_at = 0.0
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Main client loop."""
        url = f"{self.gateway_url}/sessions/{self.session_id}/realtime"
        if self.push_to_talk is not None:
            self.push_to_talk.start()
        try:
            async with websockets.connect(url) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "kind": "connect",
                            "session_id": self.session_id,
                        }
                    )
                )
                self._print_startup_hint()
                tasks = [
                    asyncio.create_task(self._receive_loop(ws), name="realtime-receive"),
                    asyncio.create_task(self._send_text(ws), name="realtime-text"),
                ]
                if self.audio_enabled and self.audio_recorder is not None:
                    tasks.append(
                        asyncio.create_task(self._send_audio(ws), name="realtime-audio")
                    )
                try:
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    self._stop.set()
                    for task in pending:
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.gather(*pending)
                    for task in done:
                        exc = task.exception()
                        if exc is not None and not isinstance(exc, asyncio.CancelledError):
                            raise exc
                finally:
                    with contextlib.suppress(Exception):
                        await ws.close()
        except websockets.exceptions.InvalidStatus as exc:
            status = getattr(exc, "status_code", getattr(exc, "status", "unknown"))
            raise RealtimeClientError(
                f"Failed to connect to {url}: HTTP {status}"
            ) from exc
        except Exception as exc:
            raise RealtimeClientError(f"Realtime client error: {exc}") from exc
        finally:
            if self.push_to_talk is not None:
                self.push_to_talk.stop()

    async def _receive_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                if self._stop.is_set():
                    return
                await self._handle_server_frame(raw)
        except websockets.exceptions.ConnectionClosed:
            return

    def _mic_open(self) -> bool:
        """Return True when the mic should stream audio to the gateway."""
        if self._stop.is_set():
            return False
        if self._model_speaking:
            return False
        if (time.monotonic() - self._last_model_audio_at) < _POST_SPEAK_MUTE_SEC:
            return False
        if self.push_to_talk is not None:
            return bool(self.push_to_talk.is_held())
        return True

    async def _handle_server_frame(self, raw: Data) -> None:
        if isinstance(raw, bytes):
            self._model_speaking = True
            self._last_model_audio_at = time.monotonic()
            if self.audio_player is not None:
                await asyncio.to_thread(self.audio_player.write, raw)
            return
        try:
            frame = parse_server_frame(raw)
        except ProtocolError as exc:
            logger.warning("ignored malformed server frame: %s", exc)
            return

        if isinstance(frame, ServerConnectedFrame):
            logger.info(
                "connected: session_id=%s input_format=%s output_format=%s chunk_ms=%s",
                frame.session_id,
                frame.input_format,
                frame.output_format,
                frame.chunk_ms,
            )
            print(f"[connected] {frame.session_id}", file=sys.stderr)
        elif isinstance(frame, ServerTextDeltaFrame):
            print(frame.delta, end="", flush=True)
            if frame.is_final:
                print()
        elif isinstance(frame, ServerTurnBoundaryFrame):
            if frame.role == "assistant":
                self._model_speaking = False
                self._last_model_audio_at = time.monotonic()
            print(f"[turn boundary: {frame.role}]")
        elif isinstance(frame, ServerToolCallFrame):
            print(f"[tool call] {frame.name}({json.dumps(frame.args)})")
        elif isinstance(frame, ServerInterruptedFrame):
            self._model_speaking = False
            print("[interrupted]")
        elif isinstance(frame, ServerErrorFrame):
            print(f"[error] {frame.error}", file=sys.stderr)
        elif isinstance(frame, ServerSessionEndedFrame):
            print(f"[session ended] {frame.reason}", file=sys.stderr)
            self._stop.set()

    def _print_startup_hint(self) -> None:
        if self.audio_enabled and self.audio_recorder is not None:
            if self.push_to_talk is not None:
                label = getattr(self.push_to_talk, "key_label", "the talk key")
                print(
                    f"Hold {label} to talk. Release to stop. "
                    "Type /bye to exit, or /help for commands.",
                    flush=True,
                )
            else:
                print(
                    "Listening... speak now. Type /bye to exit, or /help for commands.",
                    flush=True,
                )
        else:
            print(
                "Type a message and press Enter. Type /bye to exit. "
                "Commands: /help, /interrupt, /bye.",
                flush=True,
            )

    def _print_help(self) -> None:
        print("Commands:")
        print("  /help       - show this help")
        print("  /interrupt  - interrupt the model")
        print("  /bye        - close the session and exit (/quit and /exit also work)")
        if self.push_to_talk is not None:
            label = getattr(self.push_to_talk, "key_label", "the talk key")
            print(f"  Hold {label} to talk (push-to-talk)")
        print("Or just type a message and press Enter.", flush=True)

    async def _send_audio(self, ws: Any) -> None:
        if self.audio_recorder is None:
            return
        first = True
        chunk_index = 0
        muted_logged = False
        was_open = False
        while not self._stop.is_set():
            chunk = await asyncio.to_thread(self.audio_recorder.read_chunk)
            if self._stop.is_set():
                return
            mic_open = self._mic_open()
            if was_open and not mic_open and not self._stop.is_set():
                # Push-to-talk release (or model started speaking): end the utterance
                # so Gemini responds immediately instead of waiting on VAD.
                with contextlib.suppress(Exception):
                    await ws.send(_encode_client_frame(ClientAudioStreamEndFrame()))
                if self.verbose:
                    logger.debug("sent audio_stream_end after talk key release")
            was_open = mic_open
            if not mic_open:
                if self.verbose and not muted_logged:
                    logger.debug("mic closed (push-to-talk released or model speaking)")
                    muted_logged = True
                await asyncio.sleep(0)
                continue
            muted_logged = False
            if hasattr(self.audio_recorder, "chunk_peak_db"):
                level = self.audio_recorder.chunk_peak_db(chunk)
                if level < _MIC_ENERGY_THRESHOLD_DB:
                    await asyncio.sleep(0)
                    continue
            else:
                level = None
            if first:
                logger.info("microphone active: sending audio chunks while talk key is held")
                first = False
            await ws.send(chunk)
            chunk_index += 1
            if self.verbose and level is not None and chunk_index % 10 == 0:
                logger.debug("audio chunk %s peak: %.1f dBFS", chunk_index, level)
            await asyncio.sleep(0)  # yield to event loop

    async def _send_text(self, ws: Any) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                await self._request_close(ws, reason="stdin_closed")
                return
            text = line.strip()
            if not text:
                continue
            if text == "/help":
                self._print_help()
            elif text == "/interrupt":
                await ws.send(_encode_client_frame(ClientInterruptFrame()))
            elif _is_exit_command(text):
                print("Goodbye.", flush=True)
                await self._request_close(ws, reason="client_close")
                return
            else:
                await ws.send(_encode_client_frame(ClientTextFrame(text=text)))

    async def _request_close(self, ws: Any, *, reason: str) -> None:
        """Tell the gateway we are done, then stop all local loops."""
        self._stop.set()
        with contextlib.suppress(Exception):
            await ws.send(_encode_client_frame(ClientCloseFrame(reason=reason)))
        with contextlib.suppress(Exception):
            await ws.close()


def _encode_client_frame(
    frame: ClientCloseFrame | ClientInterruptFrame | ClientTextFrame | ClientAudioStreamEndFrame,
) -> str:
    """Encode a client control frame to JSON."""
    payload: dict[str, Any] = {"kind": frame.kind}
    if isinstance(frame, ClientCloseFrame):
        payload["reason"] = frame.reason
    elif isinstance(frame, ClientTextFrame):
        payload["text"] = frame.text
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
