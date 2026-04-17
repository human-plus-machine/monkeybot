"""HITLApprovalMW — drives ApprovalChannel for requires_approval actions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from ..errors import ApprovalDenied
from ..event_bus import EventBus
from ..events import EventKind, HarnessEvent, Principal, VersionTriple
from ..hitl.protocol import ApprovalChannel, ApprovalDecision, ApprovalRequest


class HITLApprovalMW:
    name = "HITLApprovalMW"

    def __init__(
        self,
        channel: ApprovalChannel,
        event_bus: EventBus,
        *,
        timeout_seconds: int = 600,
    ) -> None:
        self.channel = channel
        self.event_bus = event_bus
        self.timeout_seconds = timeout_seconds

    async def require_approval(
        self,
        *,
        run_id: str,
        session_id: str,
        principal: Principal,
        versions: VersionTriple,
        intended_action: str,
        blast_radius: str = "unspecified",
        rollback_plan: str = "unspecified",
        confidence: float = 0.5,
    ) -> ApprovalDecision:
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"
        req = ApprovalRequest(
            approval_id=approval_id,
            run_id=run_id,
            session_id=session_id,
            principal=principal,
            intended_action=intended_action,
            blast_radius=blast_radius,
            rollback_plan=rollback_plan,
            confidence=confidence,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.timeout_seconds),
        )
        await self.event_bus.publish(
            HarnessEvent(
                run_id=run_id,
                session_id=session_id,
                principal=principal,
                versions=versions,
                ts=datetime.now(UTC),
                kind=EventKind.APPROVAL_REQUEST,
                payload=req.model_dump(mode="json"),
            )
        )
        decision = await self.channel.request(req)
        await self.event_bus.publish(
            HarnessEvent(
                run_id=run_id,
                session_id=session_id,
                principal=principal,
                versions=versions,
                ts=datetime.now(UTC),
                kind=EventKind.APPROVAL_DECISION,
                payload=decision.model_dump(mode="json"),
            )
        )
        if decision.decision != "approved":
            raise ApprovalDenied(approval_id, decision.decision, decision.rationale)
        return decision
