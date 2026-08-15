"""REST control plane for prompt-first scheduled loops."""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from monkeybot.core.persistence.backends import ScheduledLoopStore, StorageBackend
from monkeybot.core.persistence.scheduled_loops import ScheduledLoopCreate, ScheduledLoopRow
from monkeybot.core.persistence.thread_summary import reserved_thread_id_error
from monkeybot.core.types.content_blocks import Text
from monkeybot.gateway.sse.loop_port import LoopPort
from monkeybot.gateway.sse.models import APIError
from monkeybot.gateway.sse.scheduler_wiring import GatewaySessionEnsurer
from monkeybot.gateway.sse.session_bus import SessionRegistry
from monkeybot.scheduler.interval import parse_interval_ms, parse_optional_duration_ms


def _get_registry(request: Request) -> SessionRegistry:
    return cast(SessionRegistry, request.app.state.registry)


def _storage_backend(request: Request) -> StorageBackend:
    backend: StorageBackend | None = getattr(request.app.state, "storage", None)
    if backend is None:
        raise APIError(503, "STORAGE_NOT_READY", "Storage backend not initialized", uuid.uuid4().hex)
    return backend


def _loop_store(request: Request) -> ScheduledLoopStore:
    return _storage_backend(request).scheduled_loops()


class CreateLoopRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    interval: str | int | float
    session_id: str = "loop-main"
    loop_id: str | None = None
    max_ticks: int | None = Field(default=None, ge=1)
    max_runtime: str | int | float | None = None
    skip_if_busy: bool = True
    unbounded: bool = False
    confirmed: bool = False


class InvokeTickRequest(BaseModel):
    session_id: str
    request_id: str
    message: str


def _row_dict(row: ScheduledLoopRow) -> dict[str, Any]:
    return {
        "loop_id": row.loop_id,
        "session_id": row.session_id,
        "status": row.status,
        "prompt": row.prompt,
        "interval_ms": row.interval_ms,
        "max_ticks": row.max_ticks,
        "max_runtime_ms": row.max_runtime_ms,
        "skip_if_busy": row.skip_if_busy,
        "tick_index": row.tick_index,
        "next_tick_at_ms": row.next_tick_at_ms,
        "started_at_ms": row.started_at_ms,
        "last_tick_at_ms": row.last_tick_at_ms,
        "last_error": row.last_error,
        "stop_reason": row.stop_reason,
        "tick_in_flight": row.tick_in_flight,
    }


def build_scheduler_router(*, loop_port: LoopPort, registry: SessionRegistry) -> APIRouter:
    router = APIRouter(prefix="/scheduler", tags=["scheduler"])
    ensurer = GatewaySessionEnsurer(registry)

    @router.post("/loops", status_code=201)
    async def create_loop(
        body: CreateLoopRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _loop_store(request)
        if not body.confirmed:
            raise APIError(
                400,
                "CONFIRMATION_REQUIRED",
                "confirmed=true is required to register a scheduled loop via REST/CLI",
                uuid.uuid4().hex,
            )
        error_message = reserved_thread_id_error(body.session_id)
        if error_message is not None:
            raise APIError(400, "BAD_REQUEST", error_message, uuid.uuid4().hex)
        try:
            interval_ms = parse_interval_ms(body.interval)
            max_runtime_ms = parse_optional_duration_ms(body.max_runtime)
        except ValueError as exc:
            raise APIError(400, "INVALID_INTERVAL", str(exc), uuid.uuid4().hex) from exc
        spec = ScheduledLoopCreate(
            prompt=body.prompt,
            interval_ms=interval_ms,
            session_id=body.session_id,
            loop_id=body.loop_id,
            max_ticks=body.max_ticks,
            max_runtime_ms=max_runtime_ms,
            skip_if_busy=body.skip_if_busy,
            unbounded=body.unbounded,
        )
        try:
            row = await store.create(spec)
        except ValueError as exc:
            msg = str(exc)
            if "scheduled loops require" in msg:
                raise APIError(400, "INVALID_GUARDS", msg, uuid.uuid4().hex) from exc
            raise APIError(409, "LOOP_EXISTS", msg, uuid.uuid4().hex) from exc
        await ensurer.ensure_session(row.session_id)
        return {"loop": _row_dict(row)}

    @router.get("/loops")
    async def list_loops(request: Request) -> dict[str, Any]:
        rows = await _loop_store(request).list_all()
        return {"loops": [_row_dict(r) for r in rows]}

    @router.get("/loops/{loop_id}")
    async def get_loop(loop_id: str, request: Request) -> dict[str, Any]:
        row = await _loop_store(request).get(loop_id)
        if row is None:
            raise APIError(404, "LOOP_NOT_FOUND", f"Unknown loop {loop_id}", uuid.uuid4().hex)
        usage = await _storage_backend(request).usage().summary(
            thread_id=row.session_id,
            since_ms=row.started_at_ms,
        )
        return {
            "loop": _row_dict(row),
            "usage": {
                "session_id": row.session_id,
                "since_ms": row.started_at_ms,
                "turns": usage.turns,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
                "cost_usd": usage.cost_usd,
            },
        }

    @router.post("/loops/{loop_id}/pause")
    async def pause_loop(loop_id: str, request: Request) -> dict[str, Any]:
        ok = await _loop_store(request).pause(loop_id)
        if not ok:
            raise APIError(404, "LOOP_NOT_FOUND", f"Unknown loop {loop_id}", uuid.uuid4().hex)
        return {"loop_id": loop_id, "status": "paused"}

    @router.post("/loops/{loop_id}/resume")
    async def resume_loop(loop_id: str, request: Request) -> dict[str, Any]:
        ok = await _loop_store(request).resume(loop_id)
        if not ok:
            raise APIError(404, "LOOP_NOT_FOUND", f"Paused loop {loop_id} not found", uuid.uuid4().hex)
        return {"loop_id": loop_id, "status": "active"}

    @router.post("/loops/{loop_id}/stop")
    async def stop_loop(loop_id: str, request: Request) -> dict[str, Any]:
        ok = await _loop_store(request).stop(loop_id)
        if not ok:
            raise APIError(404, "LOOP_NOT_FOUND", f"Unknown loop {loop_id}", uuid.uuid4().hex)
        return {"loop_id": loop_id, "status": "completed"}

    @router.post("/invoke-tick")
    async def invoke_tick_sync(
        body: InvokeTickRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(_get_registry),
    ) -> dict[str, Any]:
        """Run one agent turn synchronously (scheduler worker / internal automation)."""
        if reg_dep.get(body.session_id) is None:
            error_message = reserved_thread_id_error(body.session_id)
            if error_message is not None:
                raise APIError(400, "BAD_REQUEST", error_message, uuid.uuid4().hex)
            await ensurer.ensure_session(body.session_id)
        turn_locks = _storage_backend(request).session_turns()
        acquired = await turn_locks.try_acquire(body.session_id, body.request_id)
        if not acquired:
            raise APIError(
                409,
                "SESSION_BUSY",
                "Session already processing a request",
                uuid.uuid4().hex,
            )
        bus = reg_dep.get(body.session_id)
        if bus is not None:
            bus.current_request_id = body.request_id
        try:
            await loop_port.start_turn(
                body.session_id,
                body.request_id,
                [Text(text=body.message)],
            )
        finally:
            await turn_locks.release(body.session_id, body.request_id)
            if bus is not None and bus.current_request_id == body.request_id:
                bus.current_request_id = None
        return {"ok": True, "session_id": body.session_id, "request_id": body.request_id}

    return router
