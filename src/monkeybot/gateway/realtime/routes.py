"""FastAPI WebSocket route for MonkeyBot realtime sessions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from monkeybot.core.config.realtime_config import RealtimeConfig
from monkeybot.core.context import TurnContext, build_context
from monkeybot.core.llm.realtime_provider import (
    AudioFormat,
    RealtimeAudioDelta,
    RealtimeEvent,
    RealtimeInterrupted,
    RealtimePartialTranscript,
    RealtimeSessionConfig,
    RealtimeTextDelta,
    RealtimeToolCall,
    RealtimeTurnBoundary,
)
from monkeybot.core.llm.realtime_provider import RealtimeError as ProviderError
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.backends import HistoryStore
from monkeybot.core.runtime.realtime_loop import run_realtime_turn
from monkeybot.core.runtime.utterance_buffer import UtteranceBuffer
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse.workspace_layout import resolve_agent_workspace_root

from .deps import RealtimeDependencies
from .errors import (
    GatewayInternalError,
    GuardrailError,
    ProviderConnectionError,
    ProviderStreamError,
    RealtimeError,
)
from .guardrails import run_guardrails
from .manager import RealtimeSessionManager
from .session import RealtimeConnectionState
from .wire import (
    ClientAudioStreamEndFrame,
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
    encode_server_frame,
    parse_client_frame,
)

logger = logging.getLogger("monkeybot.gateway.realtime.routes")


def _env_context_window_tokens() -> int:
    cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "200000").strip()
    try:
        return max(1, int(cap_raw))
    except ValueError:
        return 200_000


def _resolved_workspace_paths() -> tuple[Path, Path]:
    root = resolve_agent_workspace_root()
    cwd = Path.cwd().resolve()
    skills = Path(os.environ.get("SKILLS_PATH", "skills"))
    skills_p = skills.resolve() if skills.is_absolute() else (cwd / skills).resolve()
    return root, skills_p


def _audio_duration_sec(chunk: bytes, fmt: AudioFormat) -> float:
    bytes_per_sample = 2 if fmt.encoding.startswith("pcm_s") else 1
    frame_size = bytes_per_sample * fmt.channels
    if frame_size == 0:
        return 0.0
    samples = len(chunk) // frame_size
    return samples / fmt.sample_rate_hz


async def _build_realtime_context(
    session_id: str,
    request_id: str,
    deps: RealtimeDependencies,
) -> TurnContext:
    if deps.mcp is None:
        raise RuntimeError("MCP client is not initialized")
    workspace_root, skills_path = _resolved_workspace_paths()
    agent_path = Path(os.environ.get("AGENT_MD", "monkeybot_config/AGENT.md"))
    if not agent_path.is_absolute():
        agent_path = Path.cwd().resolve() / agent_path
    model = os.environ.get("MODEL_NAME", "gemini-2.5-flash")
    return await build_context(
        thread_id=session_id,
        request_id=request_id,
        agent_md_path=agent_path,
        memory=deps.memory,
        skills_path=skills_path,
        mcp_client=deps.mcp,
        model=model,
        context_window_tokens=_env_context_window_tokens(),
        workspace_root=workspace_root,
        enable_context_curation=True,
        extra_tools=[deps.web_search_tool] if deps.web_search_tool is not None else [],
        subagent_registry=deps.subagent_registry,
    )


def _create_tool_executor(
    deps: RealtimeDependencies,
    attachment_store: Any | None = None,
    attachment_catalog: Any | None = None,
) -> CoreToolExecutor:
    if deps.mcp is None:
        raise RuntimeError("MCP client is not initialized")
    workspace_root, skills_path = _resolved_workspace_paths()
    storage = deps.storage
    return CoreToolExecutor(
        workspace_root=workspace_root,
        memory=deps.memory,
        skills_path=skills_path,
        mcp=deps.mcp,
        extra_tools=[deps.web_search_tool] if deps.web_search_tool is not None else [],
        run_command_allowed_commands=deps.run_command_allowed_commands,
        run_command_allowed_path_prefixes=deps.run_command_allowed_path_prefixes,
        attachment_store=attachment_store,
        attachment_catalog=attachment_catalog,
        run_store=storage.runs() if storage is not None else None,
        scheduled_loop_store=storage.scheduled_loops() if storage is not None else None,
        subagent_registry=deps.subagent_registry,
    )


def _make_realtime_session_config(
    ctx: TurnContext,
    realtime_config: RealtimeConfig,
) -> RealtimeSessionConfig:
    input_fmt_str = os.environ.get("MONKEYBOT_REALTIME_AUDIO_INPUT_FORMAT", "pcm_s16le_24khz_mono")
    output_fmt_str = os.environ.get("MONKEYBOT_REALTIME_AUDIO_OUTPUT_FORMAT", "pcm_s16le_24khz_mono")
    from monkeybot.core.llm.realtime_provider import AudioFormat

    def _parse(fmt: str) -> AudioFormat:
        parts = fmt.lower().split("_")
        rate = 24000
        for p in parts:
            if p.endswith("khz"):
                with contextlib.suppress(ValueError):
                    rate = int(p.replace("khz", "")) * 1000
        return AudioFormat(
            encoding="pcm_s16le" if "pcm_s16le" in fmt else "pcm_s16be",
            sample_rate_hz=rate,
            channels=1,
            frame_ms=200,
        )

    # Live-only models (e.g. gemini-3.1-flash-live-preview) cannot be used by the
    # turn-based providers, so realtime gets its own model override.
    model = realtime_config.model.name or ctx.model
    return RealtimeSessionConfig(
        model=model,
        system_prompt=ctx.agent_md,
        tools=ctx.tools,
        preferred_input_format=_parse(input_fmt_str),
        preferred_output_format=_parse(output_fmt_str),
    )


async def _send_frame(ws: WebSocket, frame: Any) -> None:
    encoded = encode_server_frame(frame)
    if isinstance(encoded, bytes):
        await ws.send_bytes(encoded)
    else:
        await ws.send_text(encoded)


async def _handle_assistant_boundary(
    state: RealtimeConnectionState,
    ctx: TurnContext,
    history: HistoryStore,
    deps: RealtimeDependencies,
    attachment_store: Any | None,
    attachment_catalog: Any | None,
) -> None:
    """Called when the assistant turn ends. Commits the turn and dispatches tools."""
    turn = state.buffer.current_assistant_turn()
    if turn.is_empty:
        return

    tool_executor = _create_tool_executor(deps, attachment_store, attachment_catalog)
    tool_results: list[Any] = []
    user_text = state.buffer.current_user_text().strip()
    try:
        async for event in run_realtime_turn(
            user_content=[Text(text=user_text)] if user_text else "",
            assistant_text=turn.text,
            assistant_tool_calls=turn.tool_calls,
            ctx=ctx,
            history=history,
            tool_executor=tool_executor,
            inspectors=deps.inspectors,
            hook_manager=deps.hook_manager,
            attachment_store=attachment_store,
            attachment_catalog=attachment_catalog,
            tool_results_out=tool_results,
        ):
            # Realtime loop events are not forwarded to the client directly; the
            # client already received the audio/text stream. Tool events are surfaced
            # as lightweight server frames so the UI can show tool execution.
            if isinstance(event, (str, str)):
                # Placeholder: actual event types imported from realtime_loop events.
                pass
    except Exception:
        logger.exception(
            "run_realtime_turn failed %s",
            kv(request_id=state.request_id, session_id=state.session_id),
        )
        return

    if tool_results:
        # Always inject immediately. Gemini Live will not continue until function
        # responses are returned for outstanding tool calls; queuing them forever
        # leaves the session hung.
        await _inject_tool_results(state, tool_results)
        if state.state == "tool_running":
            state.transition("listening")


async def _inject_tool_results(state: RealtimeConnectionState, results: list[Any]) -> None:
    """Serialize tool results and send them to the live provider session."""
    payloads: list[tuple[str, str, dict[str, object], bool]] = []
    for resp in results:
        if not hasattr(resp, "tool_name"):
            continue
        call_id = str(getattr(resp, "id", "") or "")
        name = str(getattr(resp, "tool_name", "") or "")
        is_error = bool(getattr(resp, "is_error", False))
        parts = getattr(resp, "result", []) or []
        texts = [b.text for b in parts if hasattr(b, "text") and b.text]
        if is_error:
            payload: dict[str, object] = {
                "error": "\n".join(texts) if texts else "tool execution failed",
            }
        else:
            payload = {"result": "\n".join(texts) if texts else ""}
        payloads.append((call_id, name, payload, is_error))
    if not payloads:
        return
    send_tool_results = getattr(state.realtime_session, "send_tool_results", None)
    if callable(send_tool_results):
        await send_tool_results(payloads)
        return
    # Fallback for providers without a dedicated tool-response RPC.
    summaries = [
        f"[{name}]\n{payload.get('error') or payload.get('result') or ''}"
        for _call_id, name, payload, _is_error in payloads
    ]
    await state.realtime_session.send_context("\n\n".join(summaries))


async def _process_provider_events(
    ws: WebSocket,
    state: RealtimeConnectionState,
    ctx: TurnContext,
    history: HistoryStore,
    deps: RealtimeDependencies,
    attachment_store: Any | None,
    attachment_catalog: Any | None,
) -> None:
    """Consume events from the realtime provider and forward to the client."""
    async for event in state.realtime_session.events():
        state.mark_activity(time.monotonic())
        try:
            await _handle_provider_event(
                ws,
                state,
                event,
                ctx,
                history,
                deps,
                attachment_store,
                attachment_catalog,
            )
        except RealtimeError:
            # Session-fatal error: propagate so the session loop can close cleanly.
            raise
        except Exception:
            logger.exception(
                "provider event handling failed %s",
                kv(request_id=state.request_id, session_id=state.session_id),
            )
            await _send_frame(
                ws,
                ServerErrorFrame(error="Failed to process provider event"),
            )


async def _handle_provider_event(
    ws: WebSocket,
    state: RealtimeConnectionState,
    event: RealtimeEvent,
    ctx: TurnContext,
    history: HistoryStore,
    deps: RealtimeDependencies,
    attachment_store: Any | None,
    attachment_catalog: Any | None,
) -> None:
    # Update the state machine and buffer first, then mirror the event to the client.
    state.apply_provider_event(event)
    if isinstance(event, RealtimePartialTranscript):
        await _send_frame(
            ws,
            ServerTextDeltaFrame(delta=event.text, is_final=event.is_final),
        )
    elif isinstance(event, RealtimeTextDelta):
        await _send_frame(
            ws,
            ServerTextDeltaFrame(delta=event.text, is_final=False),
        )
    elif isinstance(event, RealtimeAudioDelta):
        await _send_frame(ws, ServerAudioFrame(chunk=event.chunk))
    elif isinstance(event, RealtimeTurnBoundary):
        await _send_frame(ws, ServerTurnBoundaryFrame(role=event.role))
        if event.role == "assistant":
            await _handle_assistant_boundary(
                state, ctx, history, deps, attachment_store, attachment_catalog
            )
    elif isinstance(event, RealtimeToolCall):
        await _send_frame(
            ws,
            ServerToolCallFrame(
                call_id=event.call_id, name=event.name, args=event.args
            ),
        )
    elif isinstance(event, RealtimeInterrupted):
        await _send_frame(ws, ServerInterruptedFrame())
    elif isinstance(event, ProviderError):
        raise ProviderStreamError(event.error)


async def _handle_client_frames(
    ws: WebSocket,
    state: RealtimeConnectionState,
    input_format: AudioFormat,
) -> None:
    """Read frames from the client and forward audio/text to the provider."""
    async for raw in _receive_frames(ws):
        now = time.monotonic()
        state.mark_activity(now)
        if isinstance(raw, bytes):
            # Binary audio chunks are passed through as bytes.
            state.buffer.add_user_audio(raw, fmt=input_format)
            await state.realtime_session.send_audio(raw)
            duration = _audio_duration_sec(raw, input_format)
            state.mark_user_audio(duration)
            continue
        try:
            frame = parse_client_frame(raw)
        except ProtocolError as exc:
            await _send_frame(ws, ServerErrorFrame(error=str(exc)))
            continue

        if isinstance(frame, ClientTextFrame):
            state.buffer.add_user_text(frame.text)
            await state.realtime_session.send_text(frame.text)
        elif isinstance(frame, ClientAudioStreamEndFrame):
            # Push-to-talk release: tell the provider speech ended so it responds
            # immediately instead of waiting on VAD silence timeout.
            end_audio = getattr(state.realtime_session, "end_audio_turn", None)
            if callable(end_audio):
                await end_audio()
            state.buffer.mark_user_turn_boundary()
        elif isinstance(frame, ClientInterruptFrame):
            state.handle_user_interrupt()
            await state.realtime_session.interrupt()
            await _send_frame(ws, ServerInterruptedFrame())
        elif isinstance(frame, ClientCloseFrame):
            # Client requested a clean exit (/bye). End the reader so the session
            # loop tears down provider + guardrail tasks.
            await _send_frame(ws, ServerSessionEndedFrame(reason=frame.reason or "client_close"))
            return
        elif isinstance(frame, ClientConnectFrame):
            # Already handled before entering the frame loop.
            pass


async def _receive_frames(ws: WebSocket) -> AsyncIterator[str | bytes]:
    while True:
        try:
            data = await ws.receive()
        except WebSocketDisconnect:
            break
        if data.get("type") == "websocket.receive":
            text = data.get("text")
            if text is not None:
                yield text
            else:
                bytes_data = data.get("bytes")
                if bytes_data is not None:
                    yield bytes_data
        elif data.get("type") == "websocket.disconnect":
            break


async def _run_session(
    ws: WebSocket,
    state: RealtimeConnectionState,
    ctx: TurnContext,
    history: HistoryStore,
    deps: RealtimeDependencies,
    attachment_store: Any | None,
    attachment_catalog: Any | None,
    config: RealtimeConfig,
) -> None:
    """Main session loop: client reader, provider event handler, and guardrails."""
    provider_task = asyncio.create_task(
        _process_provider_events(
            ws, state, ctx, history, deps, attachment_store, attachment_catalog
        )
    )
    client_task = asyncio.create_task(
        _handle_client_frames(ws, state, state.realtime_session.input_format)
    )
    guardrail_task = asyncio.create_task(run_guardrails(state, config))

    all_tasks = {provider_task, client_task, guardrail_task}
    try:
        done, pending = await asyncio.wait(
            all_tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            for task in pending:
                await task
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    except asyncio.CancelledError:
        for task in all_tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await provider_task
        with contextlib.suppress(asyncio.CancelledError):
            await client_task
        with contextlib.suppress(asyncio.CancelledError):
            await guardrail_task
        raise


async def _close_session(
    state: RealtimeConnectionState,
    manager: RealtimeSessionManager,
    reason: str = "session_end",
) -> None:
    state.close()
    try:
        await state.realtime_session.close(reason=reason)
    except Exception:
        logger.exception(
            "realtime session close failed %s",
            kv(session_id=state.session_id),
        )
    state.metrics.close(reason=reason)
    state.metrics.emit_summary()
    manager.release_slot(state.session_id)
    manager.remove(state.session_id)


def create_realtime_router(
    deps: RealtimeDependencies,
    manager: RealtimeSessionManager,
) -> APIRouter:
    """Build an APIRouter with the realtime WebSocket endpoint."""
    router = APIRouter()

    @router.websocket("/sessions/{session_id}/realtime")
    async def realtime_endpoint(ws: WebSocket, session_id: str) -> None:
        request_id = os.environ.get("REQUEST_ID", "") or f"rt-{id(ws):x}"
        # Concurrency guardrail: reject the HTTP upgrade before accepting the WebSocket.
        if not await manager.acquire_slot(session_id):
            raise HTTPException(
                status_code=503,
                detail="Realtime concurrency limit reached",
                headers={"Retry-After": "10"},
            )

        # Auth placeholder: in production this should validate the upgrade request
        # using the same auth middleware as the SSE gateway (currently none exists).
        # For v1, knowing the session id is the access control.
        await ws.accept()
        logger.info(
            "realtime websocket accepted %s",
            kv(session_id=session_id, request_id=request_id),
        )

        state: RealtimeConnectionState | None = None
        close_reason = "connection_closed"
        try:
            provider = deps.realtime_provider
            if provider is None:
                raise ProviderConnectionError("Realtime provider not configured")

            storage = deps.storage
            if storage is None:
                raise GatewayInternalError("Storage backend not initialized")

            history = storage.history()
            ctx = await _build_realtime_context(session_id, request_id, deps)
            session_config = _make_realtime_session_config(ctx, manager.config)
            try:
                realtime_session = await provider.connect(config=session_config)
            except Exception as exc:
                raise ProviderConnectionError(str(exc)) from exc

            state = RealtimeConnectionState(
                session_id=session_id,
                request_id=request_id,
                provider=provider,
                realtime_session=realtime_session,
                buffer=UtteranceBuffer(),
                opened_at=time.monotonic(),
                last_activity_at=time.monotonic(),
            )
            manager.register(session_id, state)

            await _send_frame(
                ws,
                ServerConnectedFrame(
                    session_id=session_id,
                    input_format=session_config.preferred_input_format.encoding,
                    output_format=session_config.preferred_output_format.encoding,
                    chunk_ms=200,
                ),
            )

            # TODO: attach a FilesystemAttachmentStore when realtime file uploads are needed.
            attachment_store = None
            attachment_catalog = None

            await _run_session(
                ws,
                state,
                ctx,
                history,
                deps,
                attachment_store,
                attachment_catalog,
                manager.config,
            )
        except WebSocketDisconnect:
            close_reason = "client_disconnect"
        except RealtimeError as exc:
            close_reason = exc.details or exc.__class__.__name__
            level_name_to_int: dict[str, int] = {
                "debug": logging.DEBUG,
                "info": logging.INFO,
                "warning": logging.WARNING,
                "error": logging.ERROR,
                "critical": logging.CRITICAL,
            }
            logger.log(
                level_name_to_int.get(exc.log_level.lower(), logging.ERROR),
                "realtime endpoint error %s",
                kv(
                    session_id=session_id,
                    request_id=request_id,
                    error=exc.message,
                    close_reason=close_reason,
                ),
            )
            if ws.client_state.name == "CONNECTED":
                try:
                    if isinstance(exc, GuardrailError):
                        await _send_frame(
                            ws, ServerSessionEndedFrame(reason=close_reason)
                        )
                    else:
                        await _send_frame(ws, ServerErrorFrame(error=exc.message))
                    if exc.close_code:
                        await ws.close(code=exc.close_code, reason=exc.message[:123])
                except Exception:
                    pass
        except Exception as exc:
            close_reason = "error"
            logger.exception(
                "realtime endpoint error %s",
                kv(session_id=session_id, request_id=request_id),
            )
            if ws.client_state.name == "CONNECTED":
                try:
                    await _send_frame(ws, ServerErrorFrame(error=str(exc)))
                    await ws.close(code=1011, reason="Internal error")
                except Exception:
                    pass
        finally:
            if state is not None:
                await _close_session(state, manager, reason=close_reason)
            else:
                manager.release_slot(session_id)
            logger.info(
                "realtime websocket closed %s",
                kv(
                    session_id=session_id,
                    request_id=request_id,
                    close_reason=close_reason,
                ),
            )

    return router
