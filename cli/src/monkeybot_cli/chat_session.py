"""Gateway session controller for ``monkeybot chat`` (no terminal I/O)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from monkeybot.core.runtime.events import (
    ActionRequiredEvent,
    AssistantDelta,
    ContextSummarized,
    ContextSummarizing,
    ContextUsage,
    Error,
    FrontendToolRequestEvent,
    GroundingEvent,
    ThinkingBlockComplete,
    ThinkingBlockDelta,
    ToolCallResult,
    ToolCallStarted,
    ToolConfirmationRequestEvent,
    TurnComplete,
    event_from_json,
)

from monkeybot_cli.chat_status_bar import UsageStore, parse_usage_response

logger = logging.getLogger(__name__)

EmitFn = Callable[["ChatUiEvent"], None]
HitlReaderFn = Callable[["HitlRequest"], Any]  # async (HitlRequest) -> HitlAnswer

HitlKind = Literal["confirm", "elicit", "frontend_unsupported"]

_HITL_TYPES = (ToolConfirmationRequestEvent, ActionRequiredEvent, FrontendToolRequestEvent)

_RECONNECT_INITIAL_DELAY = 0.5
_RECONNECT_MAX_DELAY = 15.0
_RECONNECT_MAX_ATTEMPTS = 20
_ARGS_PREVIEW_LIMIT = 200


def pending_response_timeout_sec() -> float:
    """Mirror gateway ``PENDING_RESPONSE_TIMEOUT_SEC`` (default 300)."""
    try:
        return float(os.environ.get("PENDING_RESPONSE_TIMEOUT_SEC", "300"))
    except ValueError:
        return 300.0


def format_schema_field_lines(schema: dict[str, Any]) -> list[str]:
    """One labeled line per JSON-schema property for HITL display."""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return []
    lines: list[str] = []
    for name, raw in props.items():
        if not isinstance(name, str):
            continue
        spec = raw if isinstance(raw, dict) else {}
        typ = spec.get("type")
        type_s = str(typ) if typ is not None else "any"
        desc = spec.get("description")
        label = f"  {name} ({type_s})"
        if isinstance(desc, str) and desc.strip():
            label = f"{label} — {desc.strip()}"
        lines.append(label)
    return lines


def format_args_preview(arguments: dict[str, Any], *, limit: int = _ARGS_PREVIEW_LIMIT) -> str:
    try:
        raw = json.dumps(arguments, default=str, ensure_ascii=False)
    except TypeError:
        raw = str(arguments)
    if len(raw) > limit:
        return raw[: limit - 1] + "…"
    return raw


def extract_elicitation_fields(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Return (message, schema) from an ActionRequiredEvent payload."""
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        message = None
    else:
        message = message.strip()
    schema_raw = (
        payload.get("requestedSchema")
        or payload.get("requested_schema")
        or payload.get("schema")
    )
    schema = dict(schema_raw) if isinstance(schema_raw, dict) else None
    return message, schema


def format_hitl_plain_prompt(req: "HitlRequest") -> str:
    """Multi-line prompt for the plain (non-TTY) HITL reader."""
    parts: list[str] = [req.prompt]
    if req.kind == "confirm" and req.tool_name:
        parts.append(f"  tool: {req.tool_name}")
        if req.arguments:
            parts.append(f"  args: {format_args_preview(req.arguments)}")
    if req.schema:
        field_lines = format_schema_field_lines(req.schema)
        if field_lines:
            parts.append("  fields:")
            parts.extend(field_lines)
    if req.timeout_sec is not None and req.timeout_sec > 0:
        parts.append(f"  (timeout {int(req.timeout_sec)}s)")
    if req.kind == "confirm":
        parts.append("  [y/n]")
    return "\n".join(parts)


@dataclass(frozen=True)
class HitlRequest:
    kind: HitlKind
    prompt: str
    tool_call_id: str = ""
    elicitation_id: str = ""
    tool_name: str = ""
    schema: dict[str, Any] | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_sec: float | None = None


@dataclass(frozen=True)
class HitlAnswer:
    cancelled: bool = False
    approved: bool | None = None
    text: str = ""
    user_data: Any = None


@dataclass
class ChatUiEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _TurnState:
    assistant_started: bool = False
    done: bool = False


@dataclass
class SseFrame:
    """One SSE data payload plus optional event id from an ``id:`` line."""

    data: str
    event_id: int | None = None


def format_http_error(operation: str, exc: BaseException) -> str:
    return f"{operation} failed: {exc}"


async def iter_sse_frames(resp: httpx.Response) -> AsyncIterator[SseFrame]:
    """Yield SSE data frames, tracking ``id:`` lines for Last-Event-ID reconnect."""
    data_lines: list[str] = []
    event_id: int | None = None
    async for line in resp.aiter_lines():
        if line.startswith(":"):
            continue
        if line.startswith("id:"):
            raw = line[3:].strip()
            with contextlib.suppress(ValueError):
                event_id = int(raw)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line == "" and data_lines:
            yield SseFrame(data="\n".join(data_lines), event_id=event_id)
            data_lines = []
            event_id = None
    if data_lines:
        yield SseFrame(data="\n".join(data_lines), event_id=event_id)


async def iter_sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    """Yield SSE data payloads (compat wrapper around :func:`iter_sse_frames`)."""
    async for frame in iter_sse_frames(resp):
        yield frame.data


class ChatSessionController:
    """Owns HTTP session + SSE turn loop. Emits UI events; never writes stdout."""

    turn_based: bool = True

    def __init__(
        self,
        *,
        base: str,
        model_provider: str | None = None,
        model_name: str | None = None,
        show_thinking: bool = False,
        verbose: bool = False,
        show_usage: bool = False,
        emit: EmitFn | None = None,
        hitl_reader: HitlReaderFn | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.model_provider = model_provider
        self.model_name = model_name
        self.show_thinking = show_thinking
        self.verbose = verbose
        self.show_usage = show_usage
        self._emit_fn = emit or (lambda _e: None)
        self._hitl_reader = hitl_reader
        self._resume_session_id = (
            resume_session_id.strip()
            if isinstance(resume_session_id, str) and resume_session_id.strip()
            else None
        )
        self.usage = UsageStore()
        self.session_id: str | None = None
        self.stream_error = False
        self.transcript_report_dir: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._event_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._stream_task: asyncio.Task[None] | None = None
        self._stream_alive = True
        self._reconnecting = False
        self._last_event_id: int | None = None
        self._abandoned: deque[str] = deque(maxlen=64)
        self._turn_abort = asyncio.Event()
        self._active_request_id: str | None = None
        self._hitl_future: asyncio.Future[HitlAnswer] | None = None
        self._closed = False
        self._last_cancel_ok: bool | None = None
        self._cancel_tasks: set[asyncio.Task[None]] = set()

    def _emit(self, kind: str, **payload: Any) -> None:
        self._emit_fn(ChatUiEvent(kind=kind, payload=payload))

    def set_emit(self, emit: EmitFn) -> None:
        """Replace the UI event sink (used when the TUI takes ownership)."""
        self._emit_fn = emit

    def _warn(self, message: str) -> None:
        logger.warning("%s", message)

    def _session_body(self, *, session_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if session_id:
            body["session_id"] = session_id
        if self.model_provider:
            body["model_provider"] = self.model_provider
        if self.model_name:
            body["model_name"] = self.model_name
        return body

    async def connect(self, resume_session_id: str | None = None) -> None:
        """Open HTTP client (if needed), create/attach session, start SSE + backfill."""
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout)
        assert self._client is not None
        self._closed = False
        try:
            health = await self._client.get(f"{self.base}/health")
            health.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Gateway not reachable at {self.base}: {exc}") from exc

        sid = resume_session_id or self._resume_session_id
        if isinstance(sid, str) and sid.strip():
            await self._attach_or_create_session(sid.strip())
        else:
            await self._create_session()

        self._stream_alive = True
        self.stream_error = False
        self._reconnecting = False
        self._last_event_id = None
        self._stream_task = asyncio.create_task(self._consume_stream())
        self._emit("session_ready", session_id=self.session_id)
        self._emit("connection_state", state="connected")
        await self._emit_transcript_backfill()

    async def _create_session(self) -> None:
        assert self._client is not None
        try:
            create = await self._client.post(f"{self.base}/sessions", json=self._session_body())
            create.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(format_http_error("Session creation", exc)) from exc
        self.session_id = create.json()["session_id"]

    async def _attach_or_create_session(self, session_id: str) -> None:
        assert self._client is not None
        live = False
        try:
            probe = await self._client.get(f"{self.base}/sessions/{session_id}/usage")
            if probe.status_code == 200:
                live = True
            elif probe.status_code != 404:
                probe.raise_for_status()
        except httpx.HTTPError as exc:
            if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 404:
                raise RuntimeError(format_http_error("Session probe", exc)) from exc

        if live:
            self.session_id = session_id
            return

        try:
            create = await self._client.post(
                f"{self.base}/sessions",
                json=self._session_body(session_id=session_id),
            )
            if create.status_code == 409:
                self.session_id = session_id
                return
            create.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
                self.session_id = session_id
                return
            raise RuntimeError(format_http_error("Session creation", exc)) from exc
        self.session_id = create.json().get("session_id") or session_id

    async def _emit_transcript_backfill(self) -> None:
        assert self._client is not None and self.session_id is not None
        try:
            resp = await self._client.get(f"{self.base}/api/chat-history/{self.session_id}")
            if resp.status_code in (404, 503):
                return
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._warn(f"chat history backfill skipped: {exc}")
            return
        messages = data.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return
        wire: list[dict[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            text = str(item.get("text") or "")
            if role in ("user", "assistant") and text:
                wire.append({"role": role, "text": text})
        if wire:
            self._emit("transcript_backfill", messages=wire)

    async def _consume_stream(self) -> None:
        """Read SSE forever, reconnecting with backoff until closed or session gone."""
        assert self._client is not None and self.session_id is not None
        delay = _RECONNECT_INITIAL_DELAY
        attempts = 0
        first_connect = True

        while not self._closed:
            outcome = await self._pump_sse_connection(first_connect=first_connect)
            if outcome == "fatal":
                return
            if outcome == "open":
                first_connect = False
                attempts = 0
                delay = _RECONNECT_INITIAL_DELAY
            if self._closed:
                return
            attempts += 1
            if attempts > _RECONNECT_MAX_ATTEMPTS:
                self._stream_alive = False
                self.stream_error = True
                self._reconnecting = False
                self._emit(
                    "stream_failed",
                    message="Event stream lost after repeated reconnect attempts",
                    fatal=True,
                )
                await self._event_queue.put(None)
                return
            delay = await self._reconnect_backoff(attempts, delay)

    async def _pump_sse_connection(
        self, *, first_connect: bool
    ) -> Literal["open", "retry", "fatal"]:
        """Open one SSE connection and pump frames until EOF, retryable error, or fatal."""
        assert self._client is not None and self.session_id is not None
        headers: dict[str, str] = {}
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = str(self._last_event_id)
        opened = False
        try:
            async with self._client.stream(
                "GET",
                f"{self.base}/sessions/{self.session_id}/events",
                headers=headers,
            ) as resp:
                if resp.status_code == 404:
                    self._stream_alive = False
                    self.stream_error = True
                    self._reconnecting = False
                    self._emit(
                        "stream_failed",
                        message="Session not found on gateway",
                        fatal=True,
                    )
                    await self._event_queue.put(None)
                    return "fatal"
                resp.raise_for_status()
                opened = True
                if not first_connect:
                    self.stream_error = False
                    self._reconnecting = False
                    self._emit("connection_state", state="connected")
                    self._emit("session_ready", session_id=self.session_id)
                async for frame in iter_sse_frames(resp):
                    if frame.event_id is not None:
                        self._last_event_id = frame.event_id
                    await self._event_queue.put(frame.data)
            return "open" if opened else "fatal"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed:
                return "fatal"
            status = None
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
            if status == 404:
                self._stream_alive = False
                self.stream_error = True
                self._reconnecting = False
                self._emit(
                    "stream_failed",
                    message=format_http_error("Event stream", exc),
                    fatal=True,
                )
                await self._event_queue.put(None)
                return "fatal"
            logger.warning("SSE stream error; will reconnect: %s", exc)
            return "open" if opened else "retry"

    async def _reconnect_backoff(self, attempts: int, delay: float) -> float:
        self._reconnecting = True
        # Keep stream_alive True so the REPL/TUI stay up during backoff.
        self._emit(
            "connection_state",
            state="reconnecting",
            attempt=attempts,
            delay=delay,
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        return min(_RECONNECT_MAX_DELAY, delay * 2)

    def abort_turn(self) -> None:
        self._turn_abort.set()
        if self._hitl_future is not None and not self._hitl_future.done():
            self._hitl_future.set_result(HitlAnswer(cancelled=True))
        request_id = self._active_request_id
        if request_id and self._client is not None and self.session_id is not None:
            task = asyncio.create_task(self._post_cancel(request_id))
            self._cancel_tasks.add(task)
            task.add_done_callback(self._cancel_tasks.discard)

    async def _post_cancel(self, request_id: str) -> None:
        assert self._client is not None and self.session_id is not None
        try:
            resp = await self._client.post(
                f"{self.base}/sessions/{self.session_id}/cancel",
                json={"request_id": request_id},
            )
            resp.raise_for_status()
            self._last_cancel_ok = True
        except httpx.HTTPError as exc:
            self._last_cancel_ok = False
            logger.warning("cancel POST failed request_id=%s: %s", request_id, exc)

    def provide_hitl_answer(self, answer: HitlAnswer) -> None:
        if self._hitl_future is not None and not self._hitl_future.done():
            self._hitl_future.set_result(answer)

    async def submit(self, message: str) -> None:
        if self._client is None or self.session_id is None:
            raise RuntimeError("Session not connected")
        if not self._stream_alive or self._reconnecting:
            self._emit("error", message="Not connected — wait for reconnect")
            return

        request_id = str(uuid.uuid4())
        self._active_request_id = request_id
        self._last_cancel_ok = None
        self._turn_abort.clear()
        try:
            reply = await self._client.post(
                f"{self.base}/sessions/{self.session_id}/reply",
                json={"request_id": request_id, "message": message},
            )
            if reply.status_code == 409:
                self._emit("session_busy")
                return
            reply.raise_for_status()
        except httpx.HTTPError as exc:
            self._emit("error", message=format_http_error("Reply", exc))
            return

        self._emit("turn_started", request_id=request_id)
        await self._run_turn(request_id)

    async def _dequeue_payload(self) -> str | None | Literal[False]:
        """Return payload, None for stream end, False on poll timeout."""
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=0.15)
        except TimeoutError:
            return False

    def _decode_event(self, payload: str) -> Any | None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            self._warn(f"skipping malformed SSE JSON: {exc}")
            return None
        if data.get("type") == "ActiveRequests":
            return None
        try:
            return event_from_json(payload)
        except Exception as exc:
            self._warn(f"skipping unparseable event: {exc}")
            return None

    async def _run_turn(self, request_id: str) -> None:
        assert self._client is not None and self.session_id is not None
        state = _TurnState()
        while not state.done and not self._turn_abort.is_set():
            payload = await self._dequeue_payload()
            if payload is False:
                continue
            if payload is None:
                self._emit("thinking_clear")
                break
            evt = self._decode_event(payload)
            if evt is None:
                continue
            evt_rid = getattr(evt, "request_id", "") or ""
            if evt_rid and evt_rid in self._abandoned:
                continue
            await self._dispatch_turn_event(evt, request_id, state)
            if self._turn_abort.is_set() and isinstance(evt, _HITL_TYPES):
                break

        if self._turn_abort.is_set() and not state.done:
            self._abandoned.append(request_id)
            self._emit("thinking_clear")
            # Wait briefly for cancel POST to settle so the UI message is accurate.
            if self._cancel_tasks:
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait(self._cancel_tasks, timeout=0.4)
            cancel_ok = self._last_cancel_ok
            self._emit("turn_aborted", cancel_ok=cancel_ok)
        self._active_request_id = None
        if not self._stream_alive and not self._reconnecting:
            self._emit("stream_ended")

    async def _dispatch_turn_event(self, evt: Any, request_id: str, state: _TurnState) -> None:
        evt_rid = getattr(evt, "request_id", "") or ""

        if isinstance(evt, _HITL_TYPES):
            if evt_rid and evt_rid != request_id:
                return
            self._emit("thinking_clear")
            await self._handle_hitl(evt)
            if not self._turn_abort.is_set():
                self._emit("thinking", text="thinking…")
            return

        if evt_rid != request_id:
            self._maybe_thinking_trace(evt, request_id)
            return

        if isinstance(evt, GroundingEvent):
            self._emit(
                "grounding",
                sources=list(evt.sources),
                search_queries=list(evt.search_queries),
            )
            return
        if isinstance(evt, ToolCallStarted):
            state.assistant_started = False
            self._emit("thinking_clear")
            self._emit(
                "tool_started",
                tool=evt.tool,
                label=evt.label,
                args=dict(evt.args),
                call_id=evt.call_id,
            )
            return
        if isinstance(evt, ToolCallResult):
            state.assistant_started = False
            self._emit(
                "tool_finished",
                tool=evt.tool,
                error=evt.error,
                result=evt.result or "",
                verbose=self.verbose,
                call_id=evt.call_id,
            )
            return
        if isinstance(evt, ContextSummarizing):
            self._on_context_usage_hint(
                estimated_tokens=evt.estimated_tokens,
                context_window_tokens=evt.context_window_tokens,
            )
            self._emit("summarizing", tokens=evt.estimated_tokens)
            return
        if isinstance(evt, ContextUsage):
            self._on_context_usage_hint(
                estimated_tokens=evt.estimated_tokens,
                context_window_tokens=evt.context_window_tokens,
            )
            return
        if isinstance(evt, ContextSummarized):
            self._emit("summarized", turns=evt.turns_summarized)
            await self._fetch_usage()
            return
        if isinstance(evt, AssistantDelta):
            self._on_assistant_delta(evt, state)
            return
        if isinstance(evt, ThinkingBlockDelta):
            self._on_thinking_block_delta(evt)
            return
        if isinstance(evt, ThinkingBlockComplete):
            self._emit("thinking_block_complete")
            return
        if isinstance(evt, Error):
            self._emit("thinking_clear")
            self._emit("turn_error", error=evt.error)
            state.done = True
            return
        if isinstance(evt, TurnComplete):
            await self._emit_turn_complete(evt)
            state.done = True
            return

        self._maybe_thinking_trace(evt, request_id)

    def _on_context_usage_hint(
        self, *, estimated_tokens: int, context_window_tokens: int
    ) -> None:
        self.usage.update_context_hint(
            estimated_prompt_tokens=estimated_tokens,
            context_window_tokens=context_window_tokens,
        )
        self._emit("usage_updated", usage=self.usage.usage)

    def _on_assistant_delta(self, evt: AssistantDelta, state: _TurnState) -> None:
        if not state.assistant_started:
            self._emit("thinking_clear")
            self._emit("assistant_start")
            state.assistant_started = True
        self._emit("assistant_delta", delta=evt.delta)

    def _on_thinking_block_delta(self, evt: ThinkingBlockDelta) -> None:
        text = evt.text or ""
        if not text:
            return
        # Clear the transient spinner once real thinking text arrives.
        self._emit("thinking_clear")
        self._emit("thinking_block_delta", text=text)
        if self.show_thinking:
            self._emit("thinking_trace", text=text)

    def _maybe_thinking_trace(self, evt: Any, request_id: str) -> None:
        if not self.show_thinking:
            return
        evt_rid = getattr(evt, "request_id", "") or ""
        if evt_rid != request_id:
            return
        kind = getattr(evt, "kind", "")
        if kind not in ("Thinking", "ThinkingBlockDelta"):
            return
        text = getattr(evt, "text", "") or getattr(evt, "delta", "")
        if text:
            self._emit("thinking_trace", text=text)

    async def _emit_turn_complete(self, evt: TurnComplete) -> None:
        self._emit("thinking_clear")
        usage_payload = None
        if self.show_usage and evt.usage is not None:
            u = evt.usage
            usage_payload = {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cost_usd": u.cost_usd,
                "duration_ms": u.duration_ms,
            }
        self._emit("turn_complete", usage=usage_payload)
        await self._fetch_usage()

    async def _handle_hitl(self, evt: Any) -> None:
        assert self._client is not None and self.session_id is not None
        if isinstance(evt, FrontendToolRequestEvent):
            self._emit("hitl_frontend_unsupported", name=evt.name)
            return
        if isinstance(evt, ToolConfirmationRequestEvent):
            await self._handle_tool_confirm(evt)
            return
        if isinstance(evt, ActionRequiredEvent):
            await self._handle_elicit(evt)

    async def _handle_tool_confirm(self, evt: ToolConfirmationRequestEvent) -> None:
        assert self.session_id is not None
        prompt = evt.prompt or f"Approve tool {evt.tool_name}?"
        args = dict(evt.arguments) if evt.arguments else {}
        answer = await self._await_hitl(
            HitlRequest(
                kind="confirm",
                prompt=prompt,
                tool_call_id=evt.tool_call_id,
                tool_name=evt.tool_name,
                arguments=args,
                timeout_sec=pending_response_timeout_sec(),
            )
        )
        approved = (not answer.cancelled) and (
            answer.approved is True
            or (answer.text.strip().lower() in ("y", "yes") if answer.approved is None else False)
        )
        if answer.cancelled:
            approved = False
        reason = (
            "cancelled by user"
            if answer.cancelled
            else (None if approved else "denied by user")
        )
        ok = await self._post_hitl(
            f"{self.base}/sessions/{self.session_id}/tool-confirmations/{evt.tool_call_id}",
            {"approved": approved, "reason": reason},
            label="Tool confirmation",
        )
        if answer.cancelled or not ok:
            self._turn_abort.set()

    async def _handle_elicit(self, evt: ActionRequiredEvent) -> None:
        assert self.session_id is not None
        payload = dict(evt.payload) if evt.payload else {}
        message, schema = extract_elicitation_fields(payload)
        prompt = message or f"Agent requests input (id={evt.id or '?'})"
        answer = await self._await_hitl(
            HitlRequest(
                kind="elicit",
                prompt=prompt,
                elicitation_id=evt.id,
                schema=schema,
                timeout_sec=pending_response_timeout_sec(),
            )
        )
        if answer.cancelled:
            user_data: Any = {"cancelled": True}
        elif answer.user_data is not None:
            user_data = answer.user_data
        else:
            raw = answer.text.strip()
            user_data = raw
            if raw.startswith("{"):
                with contextlib.suppress(json.JSONDecodeError):
                    user_data = json.loads(raw)
        ok = await self._post_hitl(
            f"{self.base}/sessions/{self.session_id}/elicitations/{evt.id}",
            {"user_data": user_data},
            label="Elicitation",
        )
        if answer.cancelled or not ok:
            self._turn_abort.set()

    async def _await_hitl(self, req: HitlRequest) -> HitlAnswer:
        timeout = req.timeout_sec if req.timeout_sec is not None else pending_response_timeout_sec()
        self._emit(
            "hitl_required",
            hitl_kind=req.kind,
            prompt=req.prompt,
            tool_call_id=req.tool_call_id,
            elicitation_id=req.elicitation_id,
            tool_name=req.tool_name,
            schema=req.schema,
            arguments=req.arguments,
            timeout_sec=timeout,
        )
        if self._hitl_reader is not None:
            result = self._hitl_reader(req)
            if asyncio.iscoroutine(result):
                return await result
            return result  # type: ignore[return-value]
        loop = asyncio.get_running_loop()
        self._hitl_future = loop.create_future()
        try:
            return await self._hitl_future
        finally:
            self._hitl_future = None

    async def _post_hitl(self, url: str, payload: dict[str, Any], *, label: str) -> bool:
        assert self._client is not None
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            status = ""
            if isinstance(exc, httpx.HTTPStatusError):
                status = f" (HTTP {exc.response.status_code})"
            self._emit(
                "hitl_failed",
                message=(
                    f"{label} failed{status}: {exc}. "
                    "The agent may still be waiting server-side — the CLI did not "
                    "acknowledge this action."
                ),
            )
            return False

    async def _fetch_usage(self) -> None:
        assert self._client is not None and self.session_id is not None
        try:
            resp = await self._client.get(f"{self.base}/sessions/{self.session_id}/usage")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            if self.verbose or self.show_usage:
                self._warn(format_http_error("Usage", exc))
            return
        view = parse_usage_response(resp.json())
        self.usage.update(view)
        self._emit("usage_updated", usage=view)

    async def refresh_usage(self) -> None:
        if self._client is None or self.session_id is None:
            return
        await self._fetch_usage()

    async def _teardown_stream(self) -> None:
        if self._hitl_future is not None and not self._hitl_future.done():
            self._hitl_future.set_result(HitlAnswer(cancelled=True))
        self._hitl_future = None
        self._turn_abort.set()
        if self._active_request_id is not None:
            self._abandoned.append(self._active_request_id)
        self._active_request_id = None

        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._stream_task, timeout=0.5)
            self._stream_task = None

        while True:
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._event_queue = asyncio.Queue()
        self._abandoned.clear()
        self._last_event_id = None
        self._reconnecting = False
        self._stream_alive = True
        self.stream_error = False
        self.usage = UsageStore()
        self._turn_abort.clear()

    async def restart_session(self) -> None:
        """Close the current gateway session and open a fresh one (same HTTP client)."""
        if self._client is None or self._closed:
            raise RuntimeError("Session not connected")
        await self._teardown_stream()
        await self._create_session()
        self._stream_task = asyncio.create_task(self._consume_stream())
        self._emit("session_ready", session_id=self.session_id)
        self._emit("connection_state", state="connected")

    async def resume_session(self, session_id: str) -> None:
        """Switch to an existing (or recreated) session id and backfill transcript."""
        if self._client is None or self._closed:
            raise RuntimeError("Session not connected")
        sid = session_id.strip()
        if not sid:
            raise RuntimeError("Session id required")
        await self._teardown_stream()
        await self._attach_or_create_session(sid)
        self._stream_task = asyncio.create_task(self._consume_stream())
        self._emit("session_ready", session_id=self.session_id)
        self._emit("connection_state", state="connected")
        await self._emit_transcript_backfill()

    @property
    def stream_alive(self) -> bool:
        return self._stream_alive

    @property
    def reconnecting(self) -> bool:
        return self._reconnecting

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._stream_task, timeout=0.5)
        for task in list(self._cancel_tasks):
            task.cancel()
        if self._client is not None and self.session_id is not None:
            try:
                resp = await self._client.delete(f"{self.base}/sessions/{self.session_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    report = data.get("transcript_report_dir") if isinstance(data, dict) else None
                    if isinstance(report, str) and report.strip():
                        self.transcript_report_dir = report.strip()
            except Exception:
                logger.warning(
                    "session DELETE on close failed session_id=%s",
                    self.session_id,
                    exc_info=True,
                )
        if self._client is not None:
            await self._client.aclose()
            self._client = None
