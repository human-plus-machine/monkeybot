"""FastAPI router for the harness control plane & RunPackage retrieval.

Mounted at ``/harness``. Dependencies (SessionRegistry, RunPackageWriter,
ApprovalChannel, CompiledAgent) come from ``app.state`` so the consumer's
``src/main.py`` wires them:

    app.state.session_registry = compiled.session_registry
    app.state.run_package_writer = compiled.run_package_writer
    app.state.approval_channel = compiled.approval_channel
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.core.harness.control import IntrospectionReport, SessionRegistry
from src.core.harness.hitl.protocol import ApprovalDecision
from src.core.harness.runpackage import RunPackage
from src.core.harness.runpackage_writers import RunPackageWriter

router = APIRouter(prefix="/harness", tags=["harness"])


def _registry(req: Request) -> SessionRegistry:
    reg = getattr(req.app.state, "session_registry", None)
    if reg is None:
        raise HTTPException(503, "harness not mounted: app.state.session_registry missing")
    return reg


def _writer(req: Request) -> RunPackageWriter:
    w = getattr(req.app.state, "run_package_writer", None)
    if w is None:
        raise HTTPException(503, "harness not mounted: app.state.run_package_writer missing")
    return w


def _approval_channel(req: Request) -> Any:
    a = getattr(req.app.state, "approval_channel", None)
    if a is None:
        raise HTTPException(503, "harness not mounted: app.state.approval_channel missing")
    return a


@router.post("/control/{session_id}/pause")
async def pause(session_id: str, req: Request) -> dict:
    st = await _registry(req).pause(session_id)
    return {"status": st.status, "session_id": st.session_id}


@router.post("/control/{session_id}/resume")
async def resume(session_id: str, req: Request) -> dict:
    st = await _registry(req).resume(session_id)
    return {"status": st.status, "session_id": st.session_id}


@router.post("/control/{session_id}/cancel")
async def cancel(session_id: str, req: Request) -> dict:
    st = await _registry(req).cancel(session_id)
    return {"status": st.status, "session_id": st.session_id}


class RewindBody(BaseModel):
    checkpoint_id: str


@router.post("/control/{session_id}/rewind")
async def rewind(session_id: str, body: RewindBody, req: Request) -> dict:
    try:
        await _registry(req).rewind(session_id, body.checkpoint_id)
    except Exception as exc:
        raise HTTPException(404, f"rewind failed: {exc}") from exc
    return {"status": "rewound", "session_id": session_id, "checkpoint_id": body.checkpoint_id}


class RevokeBody(BaseModel):
    reason: str


@router.post("/control/{session_id}/revoke")
async def revoke(session_id: str, body: RevokeBody, req: Request) -> dict:
    st = await _registry(req).revoke(session_id, body.reason)
    return {"status": st.status, "session_id": st.session_id, "reason": body.reason}


@router.get("/control/sessions")
async def list_sessions(
    req: Request, principal: str | None = None, status: str | None = None
) -> list[dict]:
    sessions = await _registry(req).list_sessions(principal=principal, status=status)  # type: ignore[arg-type]
    return [
        {
            "session_id": s.session_id,
            "agent_name": s.agent_name,
            "status": s.status,
            "principal": s.principal.model_dump(mode="json"),
            "created_at": s.created_at.isoformat(),
        }
        for s in sessions
    ]


@router.get("/introspect/{session_id}", response_model=IntrospectionReport)
async def introspect(session_id: str, req: Request) -> IntrospectionReport:
    try:
        return await _registry(req).introspect(session_id)
    except KeyError as exc:
        raise HTTPException(404, f"unknown session {session_id}") from exc


@router.get("/introspect")
async def introspect_self(req: Request) -> dict:
    compiled = getattr(req.app.state, "compiled_agent", None)
    if compiled is None:
        return {"harness_version": "1", "mounted": False}
    return {
        "harness_version": "1",
        "mounted": True,
        "agent_name": compiled.harness.agent.name,
        "provider": compiled.harness.agent.provider,
        "model": compiled.harness.agent.model,
        "middleware": compiled.middleware_names(),
        "sandbox_backend": compiled.harness.sandbox.backend,
        "hitl_mode": compiled.harness.hitl.mode,
        "skills_dirs": compiled.harness.skills.dirs,
        "mcp_servers": [s.name for s in compiled.harness.mcp_servers],
        "subagents": [s.name for s in compiled.harness.subagents],
    }


@router.get("/control/approvals/pending")
async def approvals_pending(req: Request) -> list[dict]:
    channel = _approval_channel(req)
    pending = getattr(channel, "_pending", {})
    return [{"approval_id": k} for k in pending]


@router.post("/control/approvals/{approval_id}/decide")
async def approve(approval_id: str, body: ApprovalDecision, req: Request) -> dict:
    channel = _approval_channel(req)
    if not hasattr(channel, "resolve"):
        raise HTTPException(400, "approval channel does not support synchronous decide")
    decision = ApprovalDecision(
        approval_id=approval_id,
        decision=body.decision,
        rationale=body.rationale,
        decided_by=body.decided_by,
    )
    resolved = channel.resolve(decision)
    if not resolved:
        raise HTTPException(404, f"no pending approval {approval_id}")
    return {"status": "resolved", "approval_id": approval_id, "decision": decision.decision}


@router.get("/runs/{run_id}", response_model=RunPackage)
async def get_run(run_id: str, req: Request) -> RunPackage:
    pkg = await _writer(req).read(run_id)
    if pkg is None:
        raise HTTPException(404, f"run {run_id} not found")
    return pkg


@router.get("/runs")
async def list_runs(
    req: Request,
    principal: str | None = None,
    limit: int = 50,
) -> list[dict]:
    from src.core.harness.events import Principal as _P

    p = _P(kind="user", id=principal) if principal else None
    refs = await _writer(req).index(principal=p, limit=limit)
    return [r.model_dump(mode="json") for r in refs]
