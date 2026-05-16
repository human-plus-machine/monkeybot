"""
FastAPI routes and app factory for the v2 SSE gateway.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from monkeybot.core.types.content_blocks import ContentBlock
from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService

from .loop_port import LoopPort, UsagePort
from .models import (
    APIError,
    CancelRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    ElicitationPOST,
    FrontendToolResultPOST,
    HealthResponse,
    ReplyRequest,
    ReplyResponse,
    SessionUsageResponse,
    ToolConfirmationPOST,
    error_payload_dict,
)
from .session_bus import SessionAlreadyExistsError, SessionBus, SessionRegistry
from .sse import format_active_requests, format_ping
from .workspace_layout import resolve_agent_workspace_root


def get_registry(request: Request) -> SessionRegistry:
    """FastAPI dependency returning the process-local session registry."""
    return cast(SessionRegistry, request.app.state.registry)


def _default_loop_port(registry: SessionRegistry) -> LoopPort:
    """Fallback loop that only clears busy state (no events); wire a real LoopPort in production."""

    class _DefaultLoop:
        async def start_turn(
            self,
            session_id: str,
            request_id: str,
            message: str,
        ) -> None:
            _ = (request_id, message)
            bus = registry.get(session_id)
            if bus is not None:
                bus.current_request_id = None

    return _DefaultLoop()


class _StaticUsagePort:
    """UsagePort that returns zeroed aggregates (Story 7 placeholder)."""

    async def session_usage(
        self,
        session_id: str,
        *,
        since: str | None,
    ) -> dict[str, Any]:
        _ = since
        return {
            "session_id": session_id,
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "period_start": 0,
            "period_end": 0,
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


def _playground_workspace_api_enabled() -> bool:
    """Opt out with ``MONKEYBOT_PLAYGROUND_WORKSPACE_API=0`` (or ``false`` / ``no`` / ``off``)."""
    v = os.environ.get("MONKEYBOT_PLAYGROUND_WORKSPACE_API", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _playground_workspace_root() -> Path:
    """Workspace root for listing/reads; aligned with :func:`resolve_agent_workspace_root`."""
    return resolve_agent_workspace_root()


def _workspace_exc_to_api(exc: WorkspaceError) -> APIError:
    rid = uuid.uuid4().hex
    if exc.code == "not_found":
        return APIError(404, "NOT_FOUND", str(exc), rid)
    return APIError(400, "BAD_REQUEST", str(exc), rid)


def create_app(
    *,
    loop_port: LoopPort | None = None,
    usage_port: UsagePort | None = None,
    registry: SessionRegistry | None = None,
) -> FastAPI:
    """
    Build a FastAPI app with v2 SSE routes.

    For tests, pass FakeLoopPort / custom UsagePort. Story 8 wires the real loop.
    """
    reg = registry or SessionRegistry()
    loop = loop_port or _default_loop_port(reg)
    usage = usage_port or _StaticUsagePort()

    app = FastAPI(
        title="MonkeyBot v2 Gateway",
        version="2.0.0",
    )
    app.state.registry = reg
    app.state.loop = loop
    app.state.usage = usage

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
        try:
            reg_dep.create(sid, agent_md=body.agent_md, created_at_ms=created_at_ms)
        except SessionAlreadyExistsError:
            raise APIError(
                409,
                "SESSION_ALREADY_EXISTS",
                f"Session {sid} already exists",
                uuid.uuid4().hex,
            ) from None
        return CreateSessionResponse(session_id=sid, created_at=created_at_ms)

    @api.post("/sessions/{session_id}/reply", response_model=ReplyResponse)
    async def post_reply(
        session_id: str,
        body: ReplyRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),
    ) -> ReplyResponse:
        """Accept a user message and schedule the agent loop in the background."""
        bus = reg_dep.get(session_id)
        if bus is None:
            raise APIError(
                404,
                "SESSION_NOT_FOUND",
                "Unknown session",
                uuid.uuid4().hex,
            )
        if bus.current_request_id is not None:
            raise APIError(
                409,
                "SESSION_BUSY",
                "Session already processing a request",
                uuid.uuid4().hex,
            )
        loop_ref: LoopPort = request.app.state.loop
        bus.current_request_id = body.request_id

        async def _turn() -> None:
            try:
                await loop_ref.start_turn(session_id, body.request_id, body.message)
            finally:
                bus.current_request_id = None

        asyncio.create_task(_turn())
        return ReplyResponse(request_id=body.request_id)

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

    @api.get("/api/playground/workspace/tree")
    async def playground_workspace_tree(
        path: str | None = None,
    ) -> dict[str, Any]:
        """List one directory under the gateway workspace (repo-relative ``path``)."""
        if not _playground_workspace_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Playground workspace API is disabled",
                uuid.uuid4().hex,
            )
        rel = path.strip() if path and path.strip() else None
        ws = WorkspaceFileService(_playground_workspace_root())
        try:
            entries = ws.list_directory(rel)
        except WorkspaceError as exc:
            raise _workspace_exc_to_api(exc) from exc
        display = rel if rel else "."
        return {"path": display, "entries": entries}

    @api.get("/api/playground/workspace/file")
    async def playground_workspace_file(
        path: str,
        offset: int = 1,
        limit: int | None = 200,
    ) -> dict[str, Any]:
        """Read a text slice from a file under the gateway workspace (numbered lines)."""
        if not _playground_workspace_api_enabled():
            raise APIError(
                404,
                "NOT_FOUND",
                "Playground workspace API is disabled",
                uuid.uuid4().hex,
            )
        if not path or not str(path).strip():
            raise APIError(
                400,
                "BAD_REQUEST",
                "path query parameter is required",
                uuid.uuid4().hex,
            )
        ws = WorkspaceFileService(_playground_workspace_root())
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

    app.include_router(api)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Liveness probe without authentication."""
        return HealthResponse(status="ok", version="2.0.0")

    return app
