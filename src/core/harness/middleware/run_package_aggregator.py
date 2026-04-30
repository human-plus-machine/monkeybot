"""Subscribe to :class:`~.event_bus.EventBus` and mirror events into the active RunPackage frame."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..events import EventKind, HarnessEvent
from ..run_package_accumulator import RunPackageAccumulator
from ..runpackage import ApprovalRecord, TokenAccounting, ToolCallRecord


class RunPackageAggregatorMW:
    """O(1) per event; must never block or perform I/O."""

    name = "RunPackageAggregatorMW"

    def __init__(self, accumulator: RunPackageAccumulator) -> None:
        self._accum = accumulator

    async def handle(self, event: HarnessEvent) -> None:
        frame = self._accum.current_frame()
        if frame is None:
            return
        kind = event.kind
        p = event.payload

        if kind == EventKind.LLM_RESULT:
            ta = _token_from_llm_result(event.run_id, p)
            if ta is not None:
                frame.token_trace.append(ta)
            return

        if kind == EventKind.TOOL_CALL:
            call_id = str(p.get("call_id") or p.get("id") or f"tool_{event.run_id}")
            frame.pending_tool_calls[call_id] = {
                "name": str(p.get("name") or p.get("tool") or "unknown"),
                "args_redacted": dict(p.get("args_redacted") or p.get("args") or {}),
                "tier": p.get("tier", "preapproved"),
                "source": p.get("source", "native"),
                "start_ts": event.ts,
            }
            return

        if kind == EventKind.TOOL_RESULT:
            call_id = str(p.get("call_id") or p.get("id") or "")
            pending = frame.pending_tool_calls.pop(call_id, None)
            name = str(pending["name"]) if pending else str(p.get("name") or "unknown")
            args_redacted: dict[str, Any] = (
                dict(pending["args_redacted"]) if pending else dict(p.get("args_redacted") or {})
            )
            tier = str(pending["tier"]) if pending else str(p.get("tier", "preapproved"))
            source_any = pending.get("source", "native") if pending else p.get("source", "native")
            source: Any = source_any if source_any in (
                "native",
                "skill",
                "mcp",
                "subagent",
                "sandbox_exec",
            ) else "native"
            latency_ms = int(p.get("latency_ms", 0))
            success = bool(p.get("success", True))
            summary = str(p.get("result_summary") or p.get("summary") or "")
            frame.tool_calls.append(
                ToolCallRecord(
                    call_id=call_id or f"tool_{event.run_id}",
                    name=name,
                    source=source,
                    args_redacted=args_redacted,
                    result_summary=summary,
                    tier=tier,  # type: ignore[arg-type]
                    latency_ms=latency_ms,
                    success=success,
                )
            )
            return

        if kind == EventKind.APPROVAL_DECISION:
            ar = _approval_from_payload(p, fallback_ts=event.ts)
            if ar is not None:
                frame.approvals.append(ar)
            return

        if kind in (
            EventKind.CONTEXT_SUMMARIZE,
            EventKind.CONTEXT_RESET,
            EventKind.CONTEXT_OFFLOAD,
            EventKind.RULE_VETO,
            EventKind.BUDGET_UTILIZATION,
            EventKind.SUBAGENT_SPAWN,
            EventKind.SUBAGENT_RETURN,
            EventKind.APPROVAL_REQUEST,
            EventKind.LLM_CALL,
        ):
            frame.context_events.append(event)


def _token_from_llm_result(run_id: str, p: dict[str, Any]) -> TokenAccounting | None:
    call_id = str(p.get("call_id") or f"llm_{run_id}")
    model = str(p.get("model") or "unknown")
    try:
        pt = int(p.get("prompt_tokens", 0))
        ct = int(p.get("completion_tokens", 0))
        tt = int(p.get("total_tokens", pt + ct))
    except (TypeError, ValueError):
        pt, ct, tt = 0, 0, 0
    latency_ms = int(p.get("latency_ms", 0))
    cost = p.get("cost_usd")
    cost_usd = float(cost) if cost is not None else None
    return TokenAccounting(
        call_id=call_id,
        model=model,
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=tt,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )


def _approval_from_payload(p: dict[str, Any], *, fallback_ts: datetime) -> ApprovalRecord | None:
    aid = p.get("approval_id")
    if not aid:
        return None
    requested_at_raw = p.get("requested_at")
    if isinstance(requested_at_raw, str):
        requested_at = datetime.fromisoformat(requested_at_raw.replace("Z", "+00:00"))
    elif isinstance(requested_at_raw, datetime):
        requested_at = requested_at_raw
    else:
        requested_at = fallback_ts
    decided_at_raw = p.get("decided_at")
    decided_at: datetime | None
    if isinstance(decided_at_raw, str):
        decided_at = datetime.fromisoformat(decided_at_raw.replace("Z", "+00:00"))
    elif isinstance(decided_at_raw, datetime):
        decided_at = decided_at_raw
    else:
        decided_at = datetime.now(UTC)
    decision = p.get("decision")
    if decision not in ("approved", "denied", "timeout"):
        decision = "approved"
    return ApprovalRecord(
        approval_id=str(aid),
        requested_at=requested_at,
        decided_at=decided_at,
        decided_by=None,
        decision=decision,  # type: ignore[arg-type]
        rationale=p.get("rationale"),
        intended_action=str(p.get("intended_action") or ""),
        blast_radius=str(p.get("blast_radius") or ""),
        rollback_plan=str(p.get("rollback_plan") or ""),
        confidence=p.get("confidence"),
    )
