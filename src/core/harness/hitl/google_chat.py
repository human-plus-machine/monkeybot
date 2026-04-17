"""GoogleChatApprovalChannel — posts an approval card, awaits decision.

Decisions arrive asynchronously via ``POST /harness/control/approvals/{id}/decide``
which resolves the Future held here. This keeps the agent paused without blocking
the event loop.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import httpx

from .protocol import ApprovalDecision, ApprovalRequest


class GoogleChatApprovalChannel:
    name = "google_chat"

    def __init__(self, *, webhook_url: str | None = None, timeout_seconds: float = 600.0) -> None:
        self.webhook_url = webhook_url or os.environ.get("GOOGLE_CHAT_WEBHOOK")
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[req.approval_id] = fut
        if self.webhook_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(self.webhook_url, json=self._build_card(req))
            except Exception:
                pass
        try:
            return await asyncio.wait_for(fut, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return ApprovalDecision(
                approval_id=req.approval_id,
                decision="timeout",
                rationale="no decision within approval_timeout_seconds",
            )
        finally:
            self._pending.pop(req.approval_id, None)

    def resolve(self, decision: ApprovalDecision) -> bool:
        fut = self._pending.get(decision.approval_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def _build_card(self, req: ApprovalRequest) -> dict:
        return {
            "cards_v2": [
                {
                    "cardId": f"approval-{req.approval_id}",
                    "card": {
                        "header": {"title": "Approval required", "subtitle": req.intended_action[:80]},
                        "sections": [
                            {
                                "widgets": [
                                    {"decoratedText": {"topLabel": "Action", "text": req.intended_action}},
                                    {"decoratedText": {"topLabel": "Blast radius", "text": req.blast_radius}},
                                    {"decoratedText": {"topLabel": "Rollback plan", "text": req.rollback_plan}},
                                    {"decoratedText": {"topLabel": "Confidence", "text": f"{req.confidence:.2f}"}},
                                    {
                                        "decoratedText": {
                                            "topLabel": "Expires at",
                                            "text": req.expires_at.isoformat(),
                                        }
                                    },
                                ]
                            }
                        ],
                    },
                }
            ],
            "timestamp": datetime.now(UTC).isoformat(),
        }
