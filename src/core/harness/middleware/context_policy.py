"""ContextPolicyMW — budget/summarize/hard-reset enforcement.

Emits ``budget.utilization`` per LLM call, ``context.summarize`` on crossing
``summarize_at``, and ``context.hard_reset`` on crossing ``hard_reset_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Sequence

from ..event_bus import EventBus
from ..events import EventKind, HarnessEvent, Principal, VersionTriple
from ..specs import ContextPolicySpec
from ..token_count import count_tokens

SummarizerFn = Callable[[Sequence[Any]], Awaitable[Sequence[Any]]]


class ContextPolicyMW:
    name = "ContextPolicyMW"

    def __init__(
        self,
        spec: ContextPolicySpec,
        event_bus: EventBus,
        summarizer: SummarizerFn | None = None,
        *,
        model_name: str = "gemini-2.5-flash",
    ) -> None:
        self.spec = spec
        self.event_bus = event_bus
        self.summarizer = summarizer
        self.model_name = model_name

    async def apply(
        self,
        messages: Sequence[Any],
        *,
        run_id: str,
        session_id: str,
        principal: Principal,
        versions: VersionTriple,
    ) -> list[Any]:
        used = count_tokens(self.model_name, list(messages))
        pct = used / max(1, self.spec.token_budget)

        if self.spec.emit_utilization_events:
            await self.event_bus.publish(
                HarnessEvent(
                    run_id=run_id,
                    session_id=session_id,
                    principal=principal,
                    versions=versions,
                    ts=datetime.now(UTC),
                    kind=EventKind.BUDGET_UTILIZATION,
                    payload={"used": used, "budget": self.spec.token_budget, "pct": pct},
                )
            )

        if pct >= self.spec.hard_reset_at:
            await self.event_bus.publish(
                HarnessEvent(
                    run_id=run_id,
                    session_id=session_id,
                    principal=principal,
                    versions=versions,
                    ts=datetime.now(UTC),
                    kind=EventKind.CONTEXT_RESET,
                    payload={"used": used, "pct": pct},
                )
            )
            summary_msg = {
                "role": "system",
                "content": (
                    "[harness] Context hit hard reset threshold "
                    f"({pct:.2%}); prior transcript archived. Continue from the summary below."
                ),
            }
            return [summary_msg]

        if pct >= self.spec.summarize_at and self.summarizer is not None:
            summarized = list(await self.summarizer(messages))
            await self.event_bus.publish(
                HarnessEvent(
                    run_id=run_id,
                    session_id=session_id,
                    principal=principal,
                    versions=versions,
                    ts=datetime.now(UTC),
                    kind=EventKind.CONTEXT_SUMMARIZE,
                    payload={"used_before": used, "pct": pct, "messages_after": len(summarized)},
                )
            )
            return summarized

        return list(messages)
