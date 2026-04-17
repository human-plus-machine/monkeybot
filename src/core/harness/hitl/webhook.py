"""WebhookApprovalChannel — POST the request to the consumer's URL; expect a
JSON-shaped ApprovalDecision in the response."""

from __future__ import annotations

import httpx

from .protocol import ApprovalDecision, ApprovalRequest


class WebhookApprovalChannel:
    name = "webhook"

    def __init__(self, *, url: str, timeout_seconds: float = 600.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(self.url, json=req.model_dump(mode="json"))
                resp.raise_for_status()
                data = resp.json()
                data.setdefault("approval_id", req.approval_id)
                return ApprovalDecision.model_validate(data)
        except httpx.TimeoutException:
            return ApprovalDecision(
                approval_id=req.approval_id, decision="timeout", rationale="webhook timeout"
            )
        except Exception as exc:  # noqa: BLE001
            return ApprovalDecision(
                approval_id=req.approval_id,
                decision="denied",
                rationale=f"webhook error: {type(exc).__name__}: {exc}",
            )
