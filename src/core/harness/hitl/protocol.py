"""HITL protocol + request / decision Pydantic models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ..events import Principal


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: str
    run_id: str
    session_id: str
    principal: Principal
    intended_action: str
    blast_radius: str
    rollback_plan: str
    confidence: float
    expires_at: datetime


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: str
    decision: Literal["approved", "denied", "timeout"]
    rationale: str | None = None
    decided_by: Principal | None = None


@runtime_checkable
class ApprovalChannel(Protocol):
    name: str

    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...
