"""Unit tests for HITLApprovalMW + channels."""

from __future__ import annotations

import asyncio

import pytest

from src.core.harness.errors import ApprovalDenied
from src.core.harness.event_bus import EventBus
from src.core.harness.events import Principal, VersionTriple
from src.core.harness.hitl.protocol import ApprovalDecision, ApprovalRequest
from src.core.harness.middleware.hitl import HITLApprovalMW


class _FakeChannel:
    name = "fake"

    def __init__(self, decision: str = "approved", rationale: str | None = None) -> None:
        self.decision = decision
        self.rationale = rationale
        self.calls: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self.calls.append(req)
        return ApprovalDecision(
            approval_id=req.approval_id,
            decision=self.decision,  # type: ignore[arg-type]
            rationale=self.rationale,
        )


@pytest.mark.asyncio
async def test_approved_path_passes() -> None:
    bus = EventBus(include_default_logger=False)
    channel = _FakeChannel(decision="approved")
    mw = HITLApprovalMW(channel=channel, event_bus=bus)
    decision = await mw.require_approval(
        run_id="r",
        session_id="s",
        principal=Principal(),
        versions=VersionTriple(harness="1", deep_agents="x", model="y"),
        intended_action="git push",
    )
    assert decision.decision == "approved"
    assert len(channel.calls) == 1


@pytest.mark.asyncio
async def test_denied_raises_approval_denied() -> None:
    bus = EventBus(include_default_logger=False)
    mw = HITLApprovalMW(channel=_FakeChannel(decision="denied", rationale="nope"), event_bus=bus)
    with pytest.raises(ApprovalDenied) as exc:
        await mw.require_approval(
            run_id="r",
            session_id="s",
            principal=Principal(),
            versions=VersionTriple(harness="1", deep_agents="x", model="y"),
            intended_action="x",
        )
    assert exc.value.decision == "denied"
    assert "nope" in (exc.value.rationale or "")
