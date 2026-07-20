"""
FastAPI routes and app factory for the v2 SSE gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, File, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from monkeybot.core.attachments.config import (
    ALLOWED_MIME_TYPES,
    attachments_enabled_from_env,
)
from monkeybot.core.attachments.store import (
    AttachmentSessionLimitError,
    AttachmentStore,
    AttachmentTooLargeError,
    UnsupportedAttachmentTypeError,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.context_budget import summarization_trigger_ratio_from_env
from monkeybot.core.runtime.events import QueuedInputAccepted, event_to_json
from monkeybot.core.runtime.input_admission import AdmissionQueueFullError, FollowUpItem
from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService
from monkeybot.core.types.content_blocks import ContentBlock

from .loop_port import LoopPort, UsagePort
from .models import (
    APIError,
    AdmissionAcceptedResponse,
    AttachmentUploadResponse,
    CancelRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    DeleteSessionResponse,
    ElicitationPOST,
    FrontendToolResultPOST,
    HealthResponse,
    QueueRequest,
    ReplyBodyFields,
    ReplyRequest,
    ReplyResponse,
    SessionUsageResponse,
    SteerRequest,
    ToolConfirmationPOST,
    error_payload_dict,
)
from .reply_body import ReplyBodyError, normalize_reply_to_user_content
from .scheduler_routes import build_scheduler_router
from .session_bus import SessionAlreadyExistsError, SessionBus, SessionRegistry
from .sse import format_active_requests, format_ping
from .workspace_layout import resolve_agent_workspace_root

logger = logging.getLogger(__name__)


def get_registry(request: Request) -> SessionRegistry:
    """FastAPI dependency returning the process-local session registry."""
    return cast(SessionRegistry, request.app.state.registry)


def _attachment_store(request: Request) -> AttachmentStore | None:
    return getattr(request.app.state, "attachment_store", None)


def _default_loop_port(registry: SessionRegistry) -> LoopPort:
    """Fallback loop that only clears busy state (no events); wire a real LoopPort in production."""

    class _DefaultLoop:
        async def start_turn(
            self,
            session_id: str,
            request_id: str,
            user_content: list[ContentBlock],
        ) -> None:
            _ = (request_id, user_content)
            bus = registry.get(session_id)
            if bus is not None:
                bus.current_request_id = None

    return _DefaultLoop()


def _require_bus(reg: SessionRegistry, session_id: str) -> SessionBus:
    bus = reg.get(session_id)
    if bus is None:
        raise APIError(
            404,
            "SESSION_NOT_FOUND",
            "Unknown session",
            uuid.uuid4().hex,
        )
    return bus


def _parse_user_content(
    *,
    body: ReplyBodyFields,
    session_id: str,
    request: Request,
) -> list[ContentBlock]:
    try:
        return normalize_reply_to_user_content(
            message=body.message,
            content=body.content,
            session_id=session_id,
            attachment_store=_attachment_store(request),
        )
    except ReplyBodyError as exc:
        status = 404 if exc.code == "ATTACHMENT_NOT_FOUND" else 400
        raise APIError(status, exc.code, str(exc), uuid.uuid4().hex) from exc


async def _try_acquire_turn(
    *,
    bus: SessionBus,
    storage: Any,
    session_id: str,
    request_id: str,
    busy_is_error: bool,
) -> bool:
    """Acquire the session turn lock. Return True if acquired.

    When ``busy_is_error`` is True, raise ``SESSION_BUSY`` instead of returning False.
    """
    if storage is None:
        if bus.current_request_id is not None:
            if busy_is_error:
                raise APIError(
                    409,
                    "SESSION_BUSY",
                    "Session already processing a request",
                    uuid.uuid4().hex,
                )
            return False
        return True
    acquired = await storage.session_turns().try_acquire(session_id, request_id)
    if acquired:
        return True
    if busy_is_error:
        raise APIError(
            409,
            "SESSION_BUSY",
            "Session already processing a request",
            uuid.uuid4().hex,
        )
    return False


def _schedule_turn(
    *,
    bus: SessionBus,
    loop_ref: LoopPort,
    storage: Any,
    session_id: str,
    request_id: str,
    user_content: list[ContentBlock],
) -> None:
    """Background a turn and drain follow-up queue when it finishes."""

    async def _turn() -> None:
        try:
            await loop_ref.start_turn(session_id, request_id, user_content)
        finally:
            if storage is not None:
                await storage.session_turns().release(session_id, request_id)
            if bus.current_request_id == request_id:
                bus.current_request_id = None
            await _drain_follow_up(
                bus=bus,
                loop_ref=loop_ref,
                storage=storage,
                session_id=session_id,
            )

    task = asyncio.create_task(_turn())
    bus.active_turn_task = task

    def _clear(done: asyncio.Task[None]) -> None:
        if bus.active_turn_task is done:
            bus.active_turn_task = None

    task.add_done_callback(_clear)


async def _drain_follow_up(
    *,
    bus: SessionBus,
    loop_ref: LoopPort,
    storage: Any,
    session_id: str,
) -> None:
    """Start the next queued follow-up if the session is idle.

    Queues are process-local (same as ``SessionBus``); multi-replica gateways
    do not share steer/follow-up state across instances.

    When the durable turn lock cannot be acquired (e.g. held by another replica
    or a crashed claim that has not yet gone stale), the item is requeued and a
    delayed retry is scheduled. After waiting longer than the session-turn stale
    window the item is dropped so the queue cannot wedge permanently.
    """
    if bus.current_request_id is not None:
        return
    item = bus.admission.pop_follow_up()
    if item is None:
        return
    if storage is not None:
        acquired = await storage.session_turns().try_acquire(
            session_id, item.request_id
        )
        if not acquired:
            now_ms = int(time.time() * 1000)
            first_fail = item.first_lock_fail_at_ms or now_ms
            waited_ms = now_ms - first_fail
            give_up_ms = _follow_up_lock_wait_ms()
            if waited_ms >= give_up_ms:
                logger.error(
                    "follow-up dropped; turn lock held past wait budget %s",
                    kv(
                        session_id=session_id,
                        request_id=item.request_id,
                        waited_ms=waited_ms,
                        give_up_ms=give_up_ms,
                    ),
                )
                # Continue with the next queued item (if any).
                await _drain_follow_up(
                    bus=bus,
                    loop_ref=loop_ref,
                    storage=storage,
                    session_id=session_id,
                )
                return
            logger.warning(
                "follow-up requeued; lock held elsewhere %s",
                kv(
                    session_id=session_id,
                    request_id=item.request_id,
                    waited_ms=waited_ms,
                    retry_s=_follow_up_lock_retry_s(),
                ),
            )
            bus.admission.requeue_follow_up_front(
                FollowUpItem(
                    request_id=item.request_id,
                    content=item.content,
                    first_lock_fail_at_ms=first_fail,
                )
            )
            _schedule_follow_up_retry(
                bus=bus,
                loop_ref=loop_ref,
                storage=storage,
                session_id=session_id,
            )
            return
    bus.cancel_follow_up_retry()
    bus.current_request_id = item.request_id
    logger.info(
        "follow-up promoted %s",
        kv(session_id=session_id, request_id=item.request_id),
    )
    _schedule_turn(
        bus=bus,
        loop_ref=loop_ref,
        storage=storage,
        session_id=session_id,
        request_id=item.request_id,
        user_content=item.content,
    )


def _follow_up_lock_retry_s() -> float:
    """Delay between follow-up drain retries when the turn lock is held."""
    raw = os.environ.get("MONKEYBOT_FOLLOW_UP_LOCK_RETRY_S", "").strip()
    if not raw:
        return 1.0
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 1.0


def _follow_up_lock_wait_ms() -> int:
    """Max time to retry a follow-up blocked on the durable turn lock.

    Defaults to the session-turn stale window so a crashed claim can expire and
    be released on the next ``try_acquire`` before we give up.
    """
    raw = os.environ.get("MONKEYBOT_FOLLOW_UP_LOCK_WAIT_MS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        from monkeybot.core.persistence.session_turn_locks import session_turn_stale_ms

        return session_turn_stale_ms()
    except Exception:
        return 600_000


def _schedule_follow_up_retry(
    *,
    bus: SessionBus,
    loop_ref: LoopPort,
    storage: Any,
    session_id: str,
) -> None:
    """Schedule a single delayed ``_drain_follow_up`` (deduped per bus).

    When called from inside the active retry task (lock still held after a drain
    attempt), replace that task so another delay is scheduled after we return.
    """
    existing = bus.follow_up_retry_task
    current = asyncio.current_task()
    if existing is not None and not existing.done() and existing is not current:
        return

    async def _retry() -> None:
        try:
            await asyncio.sleep(_follow_up_lock_retry_s())
            await _drain_follow_up(
                bus=bus,
                loop_ref=loop_ref,
                storage=storage,
                session_id=session_id,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if bus.follow_up_retry_task is asyncio.current_task():
                bus.follow_up_retry_task = None

    bus.follow_up_retry_task = asyncio.create_task(_retry())


async def _publish_admission_accepted(
    bus: SessionBus,
    *,
    request_id: str,
    queue: Literal["steer", "follow_up"],
    position: int,
) -> AdmissionAcceptedResponse:
    await bus.publish_data(
        event_to_json(
            QueuedInputAccepted(
                request_id=request_id,
                queue=queue,
                position=position,
            )
        )
    )
    logger.info(
        "admission accepted %s",
        kv(request_id=request_id, queue=queue, position=position),
    )
    return AdmissionAcceptedResponse(
        request_id=request_id, queue=queue, position=position
    )


class _StaticUsagePort:
    """UsagePort that returns zeroed aggregates (Story 7 placeholder)."""

    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, Any]:
        _ = since
        cap_raw = os.environ.get("MODEL_CONTEXT_WINDOW", "200000").strip()
        try:
            cw = max(1, int(cap_raw))
        except ValueError:
            cw = 200_000
        st = max(1, int(cw * summarization_trigger_ratio_from_env()))
        return {
            "session_id": session_id,
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": 0.0,
            "period_start": 0,
            "period_end": 0,
            "last_prompt_tokens": 0,
            "estimated_prompt_tokens": 0,
            "summarization_threshold_tokens": st,
            "context_window_tokens": cw,
        }


async def _ping_loop(bus: SessionBus) -> None:
    """Emit `: ping N` heartbeats every 0.5s until cancelled."""
    n = 0
    try:
        while True:
            await asyncio.sleep(0.5)
            n += 1
            await bus.publish_comment(format_ping(n))
    except asyncio.CancelledError:
        raise


def _parse_last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _workspace_api_enabled() -> bool:
    """Opt out with ``MONKEYBOT_WORKSPACE_API=0`` (or ``false`` / ``no`` / ``off``)."""
    v = os.environ.get("MONKEYBOT_WORKSPACE_API", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _api_workspace_root() -> Path:
    """Workspace root for listing/reads from ``paths.workspace_root`` in monkeybot.yaml."""
    return resolve_agent_workspace_root()


def _workspace_exc_to_api(exc: WorkspaceError) -> APIError:
    rid = uuid.uuid4().hex
    if exc.code == "not_found":
        return APIError(404, "NOT_FOUND", str(exc), rid)
    return APIError(400, "BAD_REQUEST", str(exc), rid)


def _chat_history_api_enabled() -> bool:
    """Opt out with ``MONKEYBOT_CHAT_HISTORY_API=0``."""
    v = os.environ.get("MONKEYBOT_CHAT_HISTORY_API", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _storage_backend(request: Request) -> Any:
    backend = getattr(request.app.state, "storage", None)
    if backend is None:
        raise APIError(
            503,
            "STORAGE_UNAVAILABLE",
            "Storage backend is not initialized",
            uuid.uuid4().hex,
        )
    return backend


Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    *,
    loop_port: LoopPort | None = None,
    usage_port: UsagePort | None = None,
    registry: SessionRegistry | None = None,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    """
    Build a FastAPI app with v2 SSE routes.

    For tests, pass FakeLoopPort / custom UsagePort. Story 8 wires the real loop.
    """
    reg = registry or SessionRegistry(workspace_root=resolve_agent_workspace_root())
    loop = loop_port or _default_loop_port(reg)
    usage = usage_port or _StaticUsagePort()

    app = FastAPI(
        title="monkeybot v2 Gateway",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.registry = reg
    app.state.loop = loop
    app.state.usage = usage
    if not hasattr(app.state, "attachment_store"):
        app.state.attachment_store = None

    @app.exception_handler(APIError)
    async def api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload_dict(exc.code, exc.message, exc.request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        rid = uuid.uuid4().hex
        errors = exc.errors()
        message = "; ".join(str(e.get("msg", "")) for e in errors) or "Validation error"
        return JSONResponse(
            status_code=400,
            content=error_payload_dict("BAD_REQUEST", message, rid),
        )

    api = APIRouter()

    @api.post("/sessions", status_code=201, response_model=CreateSessionResponse)
    async def create_session(
        body: CreateSessionRequest,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> CreateSessionResponse:
        """Create a session and its event bus."""
        created_at_ms = int(time.time() * 1000)
        sid = body.session_id or str(uuid.uuid4())
        session_provider = None
        session_model = None
        if body.model_provider or body.model_name:
            from monkeybot.core.config.settings import get_provider_config

            try:
                cfg = get_provider_config(
                    provider=body.model_provider,
                    model_name=body.model_name,
                )
                session_provider = cfg.provider
                session_model = cfg.model
            except Exception as exc:
                raise APIError(
                    400,
                    "MODEL_UNAVAILABLE",
                    f"Model provider '{body.model_provider}' unavailable: {exc}",
                    uuid.uuid4().hex,
                ) from exc
        try:
            reg_dep.create(
                sid,
                agent_md=body.agent_md,
                created_at_ms=created_at_ms,
                provider=session_provider,
                model_name=session_model,
            )
        except SessionAlreadyExistsError:
            raise APIError(
                409,
                "SESSION_ALREADY_EXISTS",
                f"Session {sid} already exists",
                uuid.uuid4().hex,
            ) from None
        return CreateSessionResponse(session_id=sid, created_at=created_at_ms)

    @api.delete("/sessions/{session_id}", response_model=DeleteSessionResponse)
    async def delete_session(
        session_id: str,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> DeleteSessionResponse:
        """End a session: cancel pending work and free its in-process state.

        Idempotent — deleting an unknown or already-deleted session_id returns
        ``deleted=false`` rather than a 404, since the end state (no session) is
        identical. When transcripts were enabled for the session, runs offline
        analysis and returns ``transcript_report_dir``.
        """
        result = await reg_dep.remove_async(session_id)
        return DeleteSessionResponse(
            deleted=result.deleted,
            transcript_report_dir=result.transcript_report_dir,
        )

    @api.post("/sessions/{session_id}/reply", response_model=ReplyResponse)
    async def post_reply(
        session_id: str,
        body: ReplyRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> ReplyResponse:
        """Accept a user message and schedule the agent loop in the background."""
        bus = _require_bus(reg_dep, session_id)
        storage = getattr(request.app.state, "storage", None)
        await _try_acquire_turn(
            bus=bus,
            storage=storage,
            session_id=session_id,
            request_id=body.request_id,
            busy_is_error=True,
        )
        user_content = _parse_user_content(
            body=body, session_id=session_id, request=request
        )
        bus.current_request_id = body.request_id
        _schedule_turn(
            bus=bus,
            loop_ref=request.app.state.loop,
            storage=storage,
            session_id=session_id,
            request_id=body.request_id,
            user_content=user_content,
        )
        return ReplyResponse(request_id=body.request_id)

    @api.post(
        "/sessions/{session_id}/steer",
        response_model=AdmissionAcceptedResponse,
        status_code=202,
    )
    async def post_steer(
        session_id: str,
        body: SteerRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> AdmissionAcceptedResponse:
        """Enqueue mid-turn user text; injected after the current tool batch."""
        bus = _require_bus(reg_dep, session_id)
        # Capture once: subsequent awaits (parsing, enqueue) may cross a turn
        # boundary if the in-flight turn completes concurrently, so the id
        # reported back to the caller must reflect the turn that was busy at
        # acceptance time, not whatever is current when we respond.
        current_request_id = bus.current_request_id
        if current_request_id is None:
            raise APIError(
                409,
                "SESSION_IDLE",
                "Session is idle; use POST /reply instead of /steer",
                uuid.uuid4().hex,
            )
        user_content = _parse_user_content(
            body=body, session_id=session_id, request=request
        )
        try:
            position = bus.admission.enqueue_steer(user_content)
        except AdmissionQueueFullError as exc:
            logger.warning(
                "steer queue full %s",
                kv(session_id=session_id, max_size=exc.max_size),
            )
            raise APIError(
                429,
                "STEER_QUEUE_FULL",
                str(exc),
                uuid.uuid4().hex,
            ) from exc
        return await _publish_admission_accepted(
            bus,
            request_id=current_request_id,
            queue="steer",
            position=position,
        )

    @api.post(
        "/sessions/{session_id}/queue",
        response_model=AdmissionAcceptedResponse,
        status_code=202,
    )
    async def post_queue(
        session_id: str,
        body: QueueRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> AdmissionAcceptedResponse:
        """Enqueue a follow-up, or start immediately when the session is idle."""
        bus = _require_bus(reg_dep, session_id)
        user_content = _parse_user_content(
            body=body, session_id=session_id, request=request
        )
        storage = getattr(request.app.state, "storage", None)
        acquired = await _try_acquire_turn(
            bus=bus,
            storage=storage,
            session_id=session_id,
            request_id=body.request_id,
            busy_is_error=False,
        )
        if acquired:
            bus.current_request_id = body.request_id
            _schedule_turn(
                bus=bus,
                loop_ref=request.app.state.loop,
                storage=storage,
                session_id=session_id,
                request_id=body.request_id,
                user_content=user_content,
            )
            return await _publish_admission_accepted(
                bus,
                request_id=body.request_id,
                queue="follow_up",
                position=0,
            )
        try:
            position = bus.admission.enqueue_follow_up(body.request_id, user_content)
        except AdmissionQueueFullError as exc:
            logger.warning(
                "follow-up queue full %s",
                kv(session_id=session_id, max_size=exc.max_size),
            )
            raise APIError(
                429,
                "FOLLOW_UP_QUEUE_FULL",
                str(exc),
                uuid.uuid4().hex,
            ) from exc
        # Idle locally but durable lock held elsewhere: schedule retries so the
        # queue cannot sit forever waiting for a turn-complete that never comes
        # on this replica.
        _schedule_follow_up_retry(
            bus=bus,
            loop_ref=request.app.state.loop,
            storage=storage,
            session_id=session_id,
        )
        return await _publish_admission_accepted(
            bus,
            request_id=body.request_id,
            queue="follow_up",
            position=position,
        )

    @api.post(
        "/sessions/{session_id}/attachments",
        status_code=201,
        response_model=AttachmentUploadResponse,
    )
    async def post_attachment(
        session_id: str,
        request: Request,
        file: UploadFile = File(...),
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> AttachmentUploadResponse:
        if not attachments_enabled_from_env():
            raise APIError(404, "NOT_FOUND", "Attachments are disabled", uuid.uuid4().hex)
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(404, "SESSION_NOT_FOUND", "Unknown session", uuid.uuid4().hex)
        store = _attachment_store(request)
        if store is None:
            raise APIError(404, "NOT_FOUND", "Attachments are disabled", uuid.uuid4().hex)
        filename = (file.filename or "upload").strip() or "upload"
        mime_type = (file.content_type or "application/octet-stream").strip()
        if mime_type not in ALLOWED_MIME_TYPES:
            raise APIError(
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                f"Unsupported mime type: {mime_type}",
                uuid.uuid4().hex,
            )
        data = await file.read()
        try:
            stored = store.save(
                session_id,
                data=data,
                mime_type=mime_type,
                filename=filename,
            )
        except AttachmentTooLargeError as exc:
            raise APIError(413, "PAYLOAD_TOO_LARGE", str(exc), uuid.uuid4().hex) from exc
        except UnsupportedAttachmentTypeError as exc:
            raise APIError(415, "UNSUPPORTED_MEDIA_TYPE", str(exc), uuid.uuid4().hex) from exc
        except AttachmentSessionLimitError as exc:
            raise APIError(400, "BAD_REQUEST", str(exc), uuid.uuid4().hex) from exc
        return AttachmentUploadResponse(
            attachment_id=stored.attachment_id,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            filename=stored.filename,
            created_at=stored.created_at_ms,
        )

    @api.get("/sessions/{session_id}/events")
    async def stream_events(
        session_id: str,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> StreamingResponse:
        """SSE stream with replay, ActiveRequests snapshot, and ping heartbeats."""
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(
                404,
                "SESSION_NOT_FOUND",
                "Unknown session",
                uuid.uuid4().hex,
            )
        last_id = _parse_last_event_id(request)

        async def gen() -> AsyncIterator[bytes]:
            replay, q = await bus.subscribe(last_id)
            ping_task = asyncio.create_task(_ping_loop(bus))
            try:
                for frame in replay:
                    yield frame.encode("utf-8")
                active_ids = [bus.current_request_id] if bus.current_request_id else []
                yield format_active_requests(active_ids).encode("utf-8")
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        frame = await asyncio.wait_for(q.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    yield frame.encode("utf-8")
            except asyncio.CancelledError:
                raise
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                await bus.unsubscribe(q)

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @api.post("/sessions/{session_id}/cancel", status_code=200)
    async def post_cancel(
        session_id: str,
        body: CancelRequest,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> Response:
        """Record cancel intent for the agent loop (integration in a later story)."""
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(
                404,
                "SESSION_NOT_FOUND",
                "Unknown session",
                uuid.uuid4().hex,
            )
        bus.cancel_requested_for = body.request_id
        bus.admission.clear_steer()
        for fut in list(bus.pending_responses.values()):
            if not fut.done():
                fut.cancel()
        bus.abandon_pending_cancel_all()
        return Response(status_code=200)

    @api.post(
        "/sessions/{session_id}/tool-confirmations/{tool_call_id}",
        status_code=202,
    )
    async def post_tool_confirmation(
        session_id: str,
        tool_call_id: str,
        body: ToolConfirmationPOST,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> dict[str, bool]:
        rid = uuid.uuid4().hex
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(404, "SESSION_NOT_FOUND", "Unknown session", rid)
        state = bus.is_pending_or_terminal(tool_call_id)
        if state == "unknown":
            raise APIError(
                404,
                "PENDING_UNKNOWN",
                "Pending response id is not registered for this session",
                rid,
            )
        if state == "terminated":
            raise APIError(
                409,
                "STALE_PENDING_RESPONSE",
                "This pending response was already resolved, cancelled, or timed out",
                rid,
            )
        payload: dict[str, Any] = {"approved": body.approved}
        if body.reason is not None:
            payload["reason"] = body.reason
        if body.always:
            payload["always"] = True
        if not bus.resolve_pending(tool_call_id, payload):
            raise APIError(
                409,
                "STALE_PENDING_RESPONSE",
                "This pending response was already resolved, cancelled, or timed out",
                rid,
            )
        return {"ok": True}

    @api.post("/sessions/{session_id}/elicitations/{elicitation_id}", status_code=202)
    async def post_elicitation(
        session_id: str,
        elicitation_id: str,
        body: ElicitationPOST,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> dict[str, bool]:
        rid = uuid.uuid4().hex
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(404, "SESSION_NOT_FOUND", "Unknown session", rid)
        state = bus.is_pending_or_terminal(elicitation_id)
        if state == "unknown":
            raise APIError(
                404,
                "PENDING_UNKNOWN",
                "Pending response id is not registered for this session",
                rid,
            )
        if state == "terminated":
            raise APIError(
                409,
                "STALE_PENDING_RESPONSE",
                "This pending response was already resolved, cancelled, or timed out",
                rid,
            )
        payload = {"user_data": body.user_data}
        if not bus.resolve_pending(elicitation_id, payload):
            raise APIError(
                409,
                "STALE_PENDING_RESPONSE",
                "This pending response was already resolved, cancelled, or timed out",
                rid,
            )
        return {"ok": True}

    @api.post(
        "/sessions/{session_id}/frontend-tool-results/{tool_call_id}",
        status_code=202,
    )
    async def post_frontend_tool_result(
        session_id: str,
        tool_call_id: str,
        body: FrontendToolResultPOST,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> dict[str, bool]:
        rid = uuid.uuid4().hex
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(404, "SESSION_NOT_FOUND", "Unknown session", rid)
        state = bus.is_pending_or_terminal(tool_call_id)
        if state == "unknown":
            raise APIError(
                404,
                "PENDING_UNKNOWN",
                "Pending response id is not registered for this session",
                rid,
            )
        if state == "terminated":
            raise APIError(
                409,
                "STALE_PENDING_RESPONSE",
                "This pending response was already resolved, cancelled, or timed out",
                rid,
            )
        parsed: list[ContentBlock] = []
        for item in body.result:
            try:
                parsed.append(ContentBlock.from_dict(item))
            except ValueError as exc:
                raise APIError(400, "BAD_REQUEST", str(exc), rid) from exc
        payload = {
            "result": [b.to_dict() for b in parsed],
            "is_error": body.is_error,
        }
        if not bus.resolve_pending(tool_call_id, payload):
            raise APIError(
                409,
                "STALE_PENDING_RESPONSE",
                "This pending response was already resolved, cancelled, or timed out",
                rid,
            )
        return {"ok": True}

    @api.get("/sessions/{session_id}/usage", response_model=SessionUsageResponse)
    async def get_usage(
        session_id: str,
        request: Request,
        since: str | None = None,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> SessionUsageResponse:
        """Return token/cost aggregates for the session (UsagePort backend)."""
        if reg_dep.get(session_id) is None:
            raise APIError(
                404,
                "SESSION_NOT_FOUND",
                "Unknown session",
                uuid.uuid4().hex,
            )
        usage_ref: UsagePort = request.app.state.usage
        raw = await usage_ref.session_usage(session_id, since=since)
        return SessionUsageResponse.model_validate(raw)

    @api.get("/api/workspace/tree")
    async def workspace_tree(
        path: str | None = None,
    ) -> dict[str, Any]:
        """List one directory under the gateway workspace (repo-relative ``path``)."""
        if not _workspace_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Workspace API is disabled",
                uuid.uuid4().hex,
            )
        rel = path.strip() if path and path.strip() else None
        ws = WorkspaceFileService(_api_workspace_root())
        try:
            entries = ws.list_directory(rel)
        except WorkspaceError as exc:
            raise _workspace_exc_to_api(exc) from exc
        display = rel if rel else "."
        return {"path": display, "entries": entries}

    @api.get("/api/workspace/file")
    async def workspace_file(
        path: str,
        offset: int = 1,
        limit: int | None = 200,
    ) -> dict[str, Any]:
        """Read a text slice from a file under the gateway workspace (numbered lines)."""
        if not _workspace_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Workspace API is disabled",
                uuid.uuid4().hex,
            )
        if not path or not str(path).strip():
            raise APIError(
                400,
                "BAD_REQUEST",
                "path query parameter is required",
                uuid.uuid4().hex,
            )
        ws = WorkspaceFileService(_api_workspace_root())
        try:
            result = ws.read_file(path.strip(), offset=offset, limit=limit)
        except WorkspaceError as exc:
            raise _workspace_exc_to_api(exc) from exc
        return {
            "path": result["path"],
            "content": result["content"],
            "start_line": result["start_line"],
            "end_line": result["end_line"],
            "total_lines": result["total_lines"],
            "truncated": result["truncated"],
        }

    @api.get("/api/memory/graph")
    async def memory_graph(
        request: Request,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Export memory-note nodes + wiki/supersedes edges for visualization.

        Pass ``refresh=true`` to rescan note files into the sidecar (used by the
        Mac app Reload button after organizer/backfill writes new wiki links).
        """
        memory = getattr(request.app.state, "memory", None)
        if memory is None:
            raise APIError(
                404,
                "NOT_FOUND",
                "Memory layer is disabled",
                uuid.uuid4().hex,
            )
        try:
            payload = await memory.export_graph(refresh=refresh)
        except Exception as exc:
            logger.exception("memory graph export failed refresh=%s: %r", refresh, exc)
            raise
        if not isinstance(payload, dict):
            payload = {"nodes": [], "edges": [], "note": "invalid graph payload"}
        nodes = payload.get("nodes")
        edges = payload.get("edges")
        logger.info(
            "memory graph export refresh=%s nodes=%s edges=%s",
            refresh,
            len(nodes) if isinstance(nodes, list) else 0,
            len(edges) if isinstance(edges, list) else 0,
        )
        return cast(dict[str, Any], payload)

    @api.get("/api/memory/note")
    async def memory_note(
        request: Request,
        path: str,
    ) -> dict[str, Any]:
        """Fetch one memory note's body for the Mac graph inspector panel."""
        memory = getattr(request.app.state, "memory", None)
        if memory is None:
            raise APIError(
                404,
                "NOT_FOUND",
                "Memory layer is disabled",
                uuid.uuid4().hex,
            )
        path_norm = (path or "").replace("\\", "/").lstrip("./").strip()
        if not path_norm:
            raise APIError(
                400,
                "BAD_REQUEST",
                "path is required",
                uuid.uuid4().hex,
            )
        try:
            payload = await memory.search_files(
                "",
                path=path_norm,
                include_retired=True,
            )
        except Exception as exc:
            logger.exception("memory note fetch failed path=%s: %r", path_norm, exc)
            raise
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list) or not hits:
            raise APIError(
                404,
                "NOT_FOUND",
                f"Memory note not found: {path_norm}",
                uuid.uuid4().hex,
            )
        hit = hits[0] if isinstance(hits[0], dict) else {}
        result = {
            "path": hit.get("path") or path_norm,
            "type": hit.get("type") or "semantic",
            "status": hit.get("status") or "active",
            "body": hit.get("body") or "",
            "body_truncated": bool(hit.get("body_truncated")),
            "links": hit.get("links") or [],
        }
        links = result["links"]
        logger.info(
            "memory note fetch path=%s type=%s links=%s",
            result["path"],
            result["type"],
            len(links) if isinstance(links, list) else 0,
        )
        return result

    @api.get("/api/chat-history")
    async def chat_history_list(
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List recent persisted chat threads."""
        if not _chat_history_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Chat history API is disabled",
                uuid.uuid4().hex,
            )
        from monkeybot.core.persistence.thread_summary import ChatThreadSummary

        backend = _storage_backend(request)
        cap = max(1, min(limit, 200))
        rows: list[ChatThreadSummary] = await backend.history().list_threads(cap)
        return {
            "threads": [
                {
                    "session_id": row.thread_id,
                    "last_message_at": row.last_message_at,
                    "message_count": row.message_count,
                    "preview": row.preview,
                }
                for row in rows
            ]
        }

    @api.get("/api/chat-history/{session_id}")
    async def chat_history_detail(
        session_id: str,
        request: Request,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Return persisted chat turns for one thread (user/assistant/thinking/tool)."""
        if not _chat_history_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Chat history API is disabled",
                uuid.uuid4().hex,
            )
        from monkeybot.core.persistence.thread_summary import messages_to_wire

        backend = _storage_backend(request)
        cap = max(1, min(limit, 500))
        messages = await backend.history().load(session_id.strip(), limit=cap)
        return {
            "session_id": session_id,
            "messages": messages_to_wire(messages, thread_id=session_id.strip()),
        }

    @api.delete("/api/chat-history/{session_id}")
    async def chat_history_delete(
        session_id: str,
        request: Request,
    ) -> dict[str, bool]:
        """Clear one transcript and any backend-specific thread summary.

        The ``deleted`` response is an idempotent wipe acknowledgment, not an
        indication that a persisted thread previously existed.
        """
        if not _chat_history_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Chat history API is disabled",
                uuid.uuid4().hex,
            )
        backend = _storage_backend(request)
        thread_id = session_id.strip()
        try:
            await backend.history().reset(thread_id, [])
        except Exception:
            logger.exception(
                "chat history delete failed %s",
                kv(session_id=thread_id),
            )
            raise
        logger.info("chat history deleted %s", kv(session_id=thread_id))
        return {"deleted": True}

    app.include_router(api)
    app.include_router(build_scheduler_router(loop_port=loop, registry=reg))

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Liveness probe without authentication."""
        return HealthResponse(status="ok", version="2.0.0")

    return app
