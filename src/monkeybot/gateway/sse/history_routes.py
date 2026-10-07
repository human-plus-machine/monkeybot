"""Session history rewrite routes: branch, truncate, fork, switch.

Rewrites require an idle session. Edit and regenerate keep the turn lock and
hand it to the normal reply scheduler so the new turn cannot race another
admission.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, Request

from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import HistoryRewritten, event_to_json
from monkeybot.core.runtime.history_rewrite import (
    HistoryAnchor,
    HistoryRewriteError,
    RewriteEffects,
    RewriteResult,
    activate_branch,
    branch_op,
    fork_session,
    truncate_active,
)
from monkeybot.core.types.content_blocks import ContentBlock

from .models import (
    APIError,
    BranchOpRequest,
    BranchOpResponse,
    ForkRequest,
    ForkResponse,
    SetActiveBranchRequest,
    TruncateRequest,
)
from .routes import _parse_user_content, _require_bus, _schedule_turn, _try_acquire_turn
from .session_bus import SessionBus, SessionRegistry

logger = logging.getLogger(__name__)

_get_registry: Any = None


def _registry(request: Request) -> SessionRegistry:
    if not callable(_get_registry):
        raise RuntimeError("history rewrite routes are not registered")
    return cast(SessionRegistry, _get_registry(request))


def _effects() -> RewriteEffects:
    from monkeybot.gateway.sse.app import gateway_runtime

    return RewriteEffects(
        tracker=gateway_runtime.progress_tracker,
        ledger=gateway_runtime.goal_ledger,
    )


def _anchor(body_anchor: Any) -> HistoryAnchor:
    return HistoryAnchor(row_index=body_anchor.row_index, fingerprint=body_anchor.fingerprint)


def _rewrite_error(exc: HistoryRewriteError) -> APIError:
    return APIError(exc.status_code, exc.code, exc.message, uuid.uuid4().hex)


async def _release_turn(storage: Any, session_id: str, request_id: str, bus: SessionBus) -> None:
    if storage is not None:
        await storage.session_turns().release(session_id, request_id)
    if bus.current_request_id == request_id:
        bus.current_request_id = None


def _require_storage(request: Request) -> Any:
    storage = getattr(request.app.state, "storage", None)
    if storage is None or getattr(storage, "branches", None) is None:
        raise APIError(
            503,
            "STORAGE_UNAVAILABLE",
            "History storage is not available",
            uuid.uuid4().hex,
        )
    return storage


async def _acquire_idle(
    *,
    bus: SessionBus,
    request: Request,
    storage: Any,
    session_id: str,
    request_id: str,
) -> None:
    """Take the turn lock and block new voice calls, or 409 ``SESSION_BUSY``.

    A voice call writes history without the turn lock and keeps the thread it
    resolved at connect, so a connecting or live call counts as busy too.
    Pair with :func:`_end_rewrite`.
    """
    realtime = getattr(request.app.state, "realtime_manager", None)
    if realtime is not None and not realtime.begin_rewrite(session_id):
        raise APIError(
            409,
            "SESSION_BUSY",
            "End the voice session before changing chat history",
            uuid.uuid4().hex,
        )
    try:
        await _try_acquire_turn(
            bus=bus,
            storage=storage,
            session_id=session_id,
            request_id=request_id,
            busy_is_error=True,
        )
    except BaseException:
        _end_rewrite(request, session_id)
        raise


def _end_rewrite(request: Request, session_id: str) -> None:
    realtime = getattr(request.app.state, "realtime_manager", None)
    if realtime is not None:
        realtime.end_rewrite(session_id)


async def _publish(
    bus: SessionBus,
    *,
    session_id: str,
    branch_id: str,
    op: str,
    request_id: str,
) -> None:
    logger.info(
        "history rewritten %s",
        kv(session_id=session_id, branch_id=branch_id, op=op, request_id=request_id),
    )
    await bus.publish_data(
        event_to_json(
            HistoryRewritten(
                request_id=request_id,
                session_id=session_id,
                branch_id=branch_id,
                op=op,
            )
        )
    )


async def _publish_and_schedule(
    *,
    bus: SessionBus,
    request: Request,
    storage: Any,
    session_id: str,
    request_id: str,
    op: str,
    result: RewriteResult,
    user_content: list[ContentBlock] | None,
    handed_off: list[bool],
) -> BranchOpResponse:
    """Publish HistoryRewritten. Edit and regenerate hand the lock to the turn."""
    bus.admission.clear_all()
    await _publish(
        bus,
        session_id=session_id,
        branch_id=result.branch_id,
        op=op,
        request_id="" if op == "rewind" else request_id,
    )
    if op == "rewind":
        return BranchOpResponse(branch_id=result.branch_id, request_id=None)
    replay = user_content if op == "edit" else result.replay_content
    if not replay:
        raise APIError(
            422,
            "TURN_BOUNDARY",
            "Nothing to send for that branch",
            uuid.uuid4().hex,
        )
    bus.current_request_id = request_id
    _schedule_turn(
        bus=bus,
        loop_ref=request.app.state.loop,
        storage=storage,
        session_id=session_id,
        request_id=request_id,
        user_content=replay,
    )
    handed_off[0] = True
    return BranchOpResponse(branch_id=result.branch_id, request_id=request_id)


async def list_branches(
    session_id: str,
    request: Request,
    reg_dep: SessionRegistry = Depends(_registry),  # noqa: B008
) -> dict[str, Any]:
    _require_bus(reg_dep, session_id)
    storage = _require_storage(request)
    records = await storage.branches().list(session_id)
    active = await storage.branches().get_active(session_id)
    if not records:
        return {
            "active_branch_id": "root",
            "branches": [
                {
                    "branch_id": "root",
                    "parent_branch_id": None,
                    "thread_id": session_id,
                    "op": None,
                    "fork_anchor": None,
                    "active": True,
                }
            ],
        }
    return {
        "active_branch_id": active.branch_id if active is not None else "root",
        "branches": [_branch_wire(record) for record in records],
    }


async def post_branch(
    session_id: str,
    body: BranchOpRequest,
    request: Request,
    reg_dep: SessionRegistry = Depends(_registry),  # noqa: B008
) -> BranchOpResponse:
    bus = _require_bus(reg_dep, session_id)
    storage = _require_storage(request)
    request_id = (body.request_id or "").strip() or uuid.uuid4().hex
    if body.op == "edit" and body.message is None and not body.content:
        raise APIError(
            400,
            "BAD_REQUEST",
            "edit requires message or content",
            uuid.uuid4().hex,
        )
    await _acquire_idle(
        bus=bus,
        request=request,
        storage=storage,
        session_id=session_id,
        request_id=request_id,
    )
    handed_off = [False]
    try:
        user_content: list[ContentBlock] | None = None
        if body.op == "edit":
            user_content = _parse_user_content(body=body, session_id=session_id, request=request)
        try:
            result = await branch_op(
                history=storage.history(),
                branches=storage.branches(),
                session_id=session_id,
                op=body.op,
                anchor=_anchor(body.anchor),
                effects=_effects(),
            )
        except HistoryRewriteError as exc:
            raise _rewrite_error(exc) from exc
        except Exception:
            logger.exception("branch op failed %s", kv(session_id=session_id, op=body.op))
            raise
        return await _publish_and_schedule(
            bus=bus,
            request=request,
            storage=storage,
            session_id=session_id,
            request_id=request_id,
            op=body.op,
            result=result,
            user_content=user_content,
            handed_off=handed_off,
        )
    finally:
        _end_rewrite(request, session_id)
        if not handed_off[0]:
            await _release_turn(storage, session_id, request_id, bus)


async def put_active_branch(
    session_id: str,
    body: SetActiveBranchRequest,
    request: Request,
    reg_dep: SessionRegistry = Depends(_registry),  # noqa: B008
) -> dict[str, str]:
    bus = _require_bus(reg_dep, session_id)
    storage = _require_storage(request)
    request_id = uuid.uuid4().hex
    await _acquire_idle(
        bus=bus,
        request=request,
        storage=storage,
        session_id=session_id,
        request_id=request_id,
    )
    try:
        record = await activate_branch(
            branches=storage.branches(),
            session_id=session_id,
            branch_id=body.branch_id,
        )
        logger.info(
            "branch switched %s",
            kv(session_id=session_id, branch_id=record.branch_id, op="switch"),
        )
        await _rebuild_catalog(bus, storage, record.thread_id)
        bus.admission.clear_all()
    except HistoryRewriteError as exc:
        raise _rewrite_error(exc) from exc
    finally:
        _end_rewrite(request, session_id)
        await _release_turn(storage, session_id, request_id, bus)
    await _publish(
        bus,
        session_id=session_id,
        branch_id=record.branch_id,
        op="switch",
        request_id="",
    )
    return {"branch_id": record.branch_id}


async def post_truncate(
    session_id: str,
    body: TruncateRequest,
    request: Request,
    reg_dep: SessionRegistry = Depends(_registry),  # noqa: B008
) -> dict[str, str]:
    bus = _require_bus(reg_dep, session_id)
    storage = _require_storage(request)
    request_id = uuid.uuid4().hex
    await _acquire_idle(
        bus=bus,
        request=request,
        storage=storage,
        session_id=session_id,
        request_id=request_id,
    )
    try:
        result = await truncate_active(
            history=storage.history(),
            branches=storage.branches(),
            session_id=session_id,
            anchor=_anchor(body.anchor),
            effects=_effects(),
        )
        bus.admission.clear_all()
    except HistoryRewriteError as exc:
        raise _rewrite_error(exc) from exc
    finally:
        _end_rewrite(request, session_id)
        await _release_turn(storage, session_id, request_id, bus)
    await _publish(
        bus,
        session_id=session_id,
        branch_id=result.branch_id,
        op="truncate",
        request_id="",
    )
    return {"branch_id": result.branch_id}


async def post_fork(
    session_id: str,
    body: ForkRequest,
    request: Request,
    reg_dep: SessionRegistry = Depends(_registry),  # noqa: B008
) -> ForkResponse:
    bus = _require_bus(reg_dep, session_id)
    storage = _require_storage(request)
    request_id = uuid.uuid4().hex
    await _acquire_idle(
        bus=bus,
        request=request,
        storage=storage,
        session_id=session_id,
        request_id=request_id,
    )
    try:
        result = await fork_session(
            history=storage.history(),
            branches=storage.branches(),
            session_id=session_id,
            anchor=_anchor(body.anchor),
            effects=_effects(),
        )
    except HistoryRewriteError as exc:
        raise _rewrite_error(exc) from exc
    finally:
        _end_rewrite(request, session_id)
        await _release_turn(storage, session_id, request_id, bus)
    if result.forked_session_id is None:
        raise APIError(500, "INTERNAL", "Fork did not create a session", uuid.uuid4().hex)
    await _publish(
        bus,
        session_id=session_id,
        branch_id=result.branch_id,
        op="fork",
        request_id="",
    )
    return ForkResponse(session_id=result.forked_session_id)


def register_history_rewrite_routes(api: APIRouter, *, get_registry: Any) -> None:
    """Attach rewrite routes to the gateway router."""
    global _get_registry
    _get_registry = get_registry
    api.get("/sessions/{session_id}/branches")(list_branches)
    api.post("/sessions/{session_id}/branches", response_model=BranchOpResponse)(post_branch)
    api.put("/sessions/{session_id}/branches/active")(put_active_branch)
    api.post("/sessions/{session_id}/truncate")(post_truncate)
    api.post("/sessions/{session_id}/fork", response_model=ForkResponse)(post_fork)


def _branch_wire(record: Any) -> dict[str, Any]:
    anchor = None
    if record.fork_fingerprint is not None and record.fork_row_index is not None:
        anchor = {"row_index": record.fork_row_index, "fingerprint": record.fork_fingerprint}
    return {
        "branch_id": record.branch_id,
        "parent_branch_id": record.parent_branch_id,
        "thread_id": record.thread_id,
        "op": record.op,
        "fork_anchor": anchor,
        "active": record.is_active,
    }


async def _rebuild_catalog(bus: SessionBus, storage: Any, thread_id: str) -> None:
    catalog = bus.attachment_catalog
    if catalog is None:
        return
    rows = await storage.history().load(thread_id)
    catalog.rebuild_from_history(rows)
