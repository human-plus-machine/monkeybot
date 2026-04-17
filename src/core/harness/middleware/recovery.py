"""RecoveryMW — retry with exponential backoff + escalation after N consecutive fails.

Converts framework errors (RuleViolation, SandboxDenied, ApprovalDenied) into
synthetic tool messages so the agent can replan rather than crashing."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..errors import ApprovalDenied, HarnessError, RuleViolation, SandboxDenied
from ..specs import AutonomySpec


@dataclass
class RecoveryMW:
    name: str = "RecoveryMW"
    spec: AutonomySpec = field(default_factory=AutonomySpec)
    _recent_failures: deque[str] = field(default_factory=lambda: deque(maxlen=16))

    async def call_with_retry(
        self,
        fn: Callable[[], Awaitable[Any]],
        *,
        error_tag: str,
    ) -> Any:
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self.spec.retry_max:
            try:
                return await fn()
            except (RuleViolation, SandboxDenied, ApprovalDenied) as exc:
                self._recent_failures.append(error_tag)
                return self._synthetic_message_for(exc)
            except HarnessError as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            attempt += 1
            await asyncio.sleep(self.spec.retry_backoff_base_seconds * (2 ** (attempt - 1)))
            self._recent_failures.append(error_tag)
        if self._should_escalate():
            raise HarnessError(
                f"recovery exhausted and escalation threshold reached for {error_tag}: {last_exc}"
            )
        if last_exc is not None:
            raise last_exc
        raise HarnessError("RecoveryMW terminated without a result and without an exception")

    def _should_escalate(self) -> bool:
        if len(self._recent_failures) < self.spec.escalate_after_consecutive_failures:
            return False
        tail = list(self._recent_failures)[-self.spec.escalate_after_consecutive_failures :]
        return len(set(tail)) == 1

    @staticmethod
    def _synthetic_message_for(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, RuleViolation):
            return {
                "role": "tool",
                "content": (
                    f"[harness] action refused by RULES.md rule {exc.rule_id}: {exc.rule_text}. "
                    f"Replan without {exc.action}."
                ),
                "tool_call_id": "harness-rule-veto",
            }
        if isinstance(exc, SandboxDenied):
            return {
                "role": "tool",
                "content": f"[harness] sandbox denied: {exc.reason}. Replan without touching {exc.resource!r}.",
                "tool_call_id": "harness-sandbox-denied",
            }
        if isinstance(exc, ApprovalDenied):
            return {
                "role": "tool",
                "content": (
                    f"[harness] approval {exc.approval_id} {exc.decision}"
                    + (f": {exc.rationale}" if exc.rationale else "")
                    + ". Do not retry the same action."
                ),
                "tool_call_id": "harness-approval-denied",
            }
        return {
            "role": "tool",
            "content": f"[harness] unrecoverable error: {type(exc).__name__}: {exc}",
            "tool_call_id": "harness-error",
        }
