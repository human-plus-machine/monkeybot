"""History routes: edit, regenerate, rewind, list, switch, truncate, and fork.

Every op needs an idle session: it takes the turn lock and blocks new voice
calls. Edit and regenerate hand the lock to the reply scheduler, so the new
turn cannot race another admission.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.branches import ROOT_BRANCH_ID, BranchRecord, root_record
from monkeybot.core.runtime.events import HistoryRewritten, event_to_json
from monkeybot.core.runtime.history_rewrite import (
    HistoryRewriteError,
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
    ForkResponse,
    HistoryAnchorRequest,
    SetActiveBranchRequest,
    TruncateResponse,
)
from .routes import (
    _attachment_store,
    _block_voice_calls,
    _drain_follow_up,
    _parse_user_content,
    _require_bus,
    _schedule_turn,
    _storage_backend,
    _try_acquire_turn,
    _unblock_voice_calls,
    get_registry,
)
from .session_bus import SessionBus, SessionRegistry

logger = logging.getLogger(__name__)


def _rewrite_error(exc: HistoryRewriteError) -> APIError:
    return APIError(exc.status_code, exc.code, exc.message, uuid.uuid4().hex)


def _branch_wire(record: BranchRecord) -> dict[str, Any]:
    return {
        "branch_id": record.branch_id,
        "parent_branch_id": record.parent_branch_id,
        "fork_anchor": {"row_id": record.fork_row_id} if record.fork_row_id else None,
        "op": record.op,
        "created_at": record.created_at,
        "last_active_at": record.last_active_at,
        "active": record.is_active,
    }


@dataclass
class _IdleLease:
    """Turn lock plus a voice-call block for one history change, or 409 ``SESSION_BUSY``.

    :meth:`hand_off` passes the turn lock to a scheduled turn, which releases it.
    """

    bus: SessionBus
    request: Request
    storage: Any
    session_id: str
    request_id: str
    handed_off: bool = False

    async def __aenter__(self) -> _IdleLease:
        _block_voice_calls(self.request, self.session_id)
        try:
            await _try_acquire_turn(
                bus=self.bus,
                storage=self.storage,
                session_id=self.session_id,
                request_id=self.request_id,
                busy_is_error=True,
            )
        except BaseException:
            _unblock_voice_calls(self.request, self.session_id)
            raise
        return self

    def hand_off(self) -> None:
        self.handed_off = True
        _unblock_voice_calls(self.request, self.session_id)

    async def __aexit__(self, *exc: object) -> None:
        if self.handed_off:
            return
        try:
            await self.storage.session_turns().release(self.session_id, self.request_id)
        finally:
            if self.bus.current_request_id == self.request_id:
                self.bus.current_request_id = None
            _unblock_voice_calls(self.request, self.session_id)


async def _publish(
    bus: SessionBus, *, session_id: str, branch_id: str, op: str, request_id: str = ""
) -> None:
    logger.info(
        "history rewritten %s",
        kv(session_id=session_id, branch_id=branch_id, op=op, request_id=request_id),
    )
    await bus.publish_data(
        event_to_json(
            HistoryRewritten(
                request_id=request_id, session_id=session_id, branch_id=branch_id, op=op
            )
        )
    )


def register_history_rewrite_routes(api: APIRouter) -> None:
    """Attach the branch routes to the gateway router."""

    @api.get("/sessions/{session_id}/branches")
    async def list_branches(
        session_id: str,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),  # noqa: B008
    ) -> dict[str, Any]:
        _require_bus(reg_dep, session_id)
        records = await _storage_backend(request).branches().list(session_id)
        if not records:
            records = [root_record(session_id, is_active=True, now=0)]
        active = next((r.branch_id for r in records if r.is_active), ROOT_BRANCH_ID)
        return {"active_branch_id": active, "branches": [_branch_wire(r) for r in records]}

    @api.post("/sessions/{session_id}/branches", response_model=BranchOpResponse)
    async def post_branch(
        session_id: str,
        body: BranchOpRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),  # noqa: B008
    ) -> BranchOpResponse:
        bus = _require_bus(reg_dep, session_id)
        storage = _storage_backend(request)
        if body.op == "edit" and body.message is None and not body.content:
            raise APIError(400, "BAD_REQUEST", "edit requires message or content", uuid.uuid4().hex)
        edited: list[ContentBlock] | None = None
        if body.op == "edit":
            edited = _parse_user_content(body=body, session_id=session_id, request=request)
        request_id = (body.request_id or "").strip() or uuid.uuid4().hex
        async with _IdleLease(bus, request, storage, session_id, request_id) as lease:
            try:
                result = await branch_op(
                    history=storage.history(),
                    branches=storage.branches(),
                    session_id=session_id,
                    op=body.op,
                    anchor_row_id=body.anchor.row_id,
                )
            except HistoryRewriteError as exc:
                raise _rewrite_error(exc) from exc
            except Exception:
                logger.exception("branch op failed %s", kv(session_id=session_id, op=body.op))
                raise
            replay = edited if body.op == "edit" else result.replay_content
            if replay is not None:
                await _publish(
                    bus,
                    session_id=session_id,
                    branch_id=result.branch_id,
                    op=body.op,
                    request_id=request_id,
                )
                bus.current_request_id = request_id
                # Follow-ups queued during the op drain when the replay ends.
                _schedule_turn(
                    bus=bus,
                    loop_ref=request.app.state.loop,
                    storage=storage,
                    session_id=session_id,
                    request_id=request_id,
                    user_content=replay,
                )
                lease.hand_off()
                return BranchOpResponse(branch_id=result.branch_id, request_id=request_id)
            await _publish(bus, session_id=session_id, branch_id=result.branch_id, op=body.op)
        # Follow-ups queued while the lease held the turn lock run on the new branch.
        await _drain_follow_up(
            bus=bus, loop_ref=request.app.state.loop, storage=storage, session_id=session_id
        )
        return BranchOpResponse(branch_id=result.branch_id)

    @api.put("/sessions/{session_id}/branches/active")
    async def put_active_branch(
        session_id: str,
        body: SetActiveBranchRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),  # noqa: B008
    ) -> dict[str, str]:
        bus = _require_bus(reg_dep, session_id)
        storage = _storage_backend(request)
        async with _IdleLease(bus, request, storage, session_id, uuid.uuid4().hex):
            try:
                record = await activate_branch(
                    branches=storage.branches(), session_id=session_id, branch_id=body.branch_id
                )
            except HistoryRewriteError as exc:
                raise _rewrite_error(exc) from exc
        await _publish(bus, session_id=session_id, branch_id=record.branch_id, op="switch")
        await _drain_follow_up(
            bus=bus, loop_ref=request.app.state.loop, storage=storage, session_id=session_id
        )
        return {"branch_id": record.branch_id}

    @api.post("/sessions/{session_id}/truncate", response_model=TruncateResponse)
    async def post_truncate(
        session_id: str,
        body: HistoryAnchorRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),  # noqa: B008
    ) -> TruncateResponse:
        bus = _require_bus(reg_dep, session_id)
        storage = _storage_backend(request)
        async with _IdleLease(bus, request, storage, session_id, uuid.uuid4().hex):
            try:
                result = await truncate_active(
                    history=storage.history(),
                    branches=storage.branches(),
                    session_id=session_id,
                    anchor_row_id=body.anchor.row_id,
                )
            except HistoryRewriteError as exc:
                raise _rewrite_error(exc) from exc
        await _publish(bus, session_id=session_id, branch_id=result.branch_id, op="truncate")
        await _drain_follow_up(
            bus=bus, loop_ref=request.app.state.loop, storage=storage, session_id=session_id
        )
        return TruncateResponse(branch_id=result.branch_id)

    @api.post("/sessions/{session_id}/fork", response_model=ForkResponse)
    async def post_fork(
        session_id: str,
        body: HistoryAnchorRequest,
        request: Request,
        reg_dep: SessionRegistry = Depends(get_registry),  # noqa: B008
    ) -> ForkResponse:
        bus = _require_bus(reg_dep, session_id)
        storage = _storage_backend(request)
        # Fork only reads this session, but holds it idle so the copied prefix
        # is never a turn caught halfway through writing its rows.
        async with _IdleLease(bus, request, storage, session_id, uuid.uuid4().hex):
            try:
                result = await fork_session(
                    history=storage.history(),
                    branches=storage.branches(),
                    attachments=_attachment_store(request),
                    session_id=session_id,
                    anchor_row_id=body.anchor.row_id,
                )
            except HistoryRewriteError as exc:
                raise _rewrite_error(exc) from exc
        # The fork keeps the source's model and instructions.
        reg_dep.create(
            result.session_id,
            agent_md=bus.agent_md,
            created_at_ms=int(time.time() * 1000),
            provider=bus.provider,
            model_name=bus.model_name,
        )
        await _drain_follow_up(
            bus=bus, loop_ref=request.app.state.loop, storage=storage, session_id=session_id
        )
        return ForkResponse(session_id=result.session_id)
