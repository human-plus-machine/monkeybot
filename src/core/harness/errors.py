"""Harness error hierarchy. All raised by framework code inherit HarnessError."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all harness errors."""


class HarnessConfigError(HarnessError):
    """Raised when HarnessConfig fails validation or references missing resources."""


class RuleViolation(HarnessError):
    """Raised by RulesEnforcementMW when an action violates RULES.md."""

    def __init__(self, rule_id: str, rule_text: str, action: str) -> None:
        self.rule_id = rule_id
        self.rule_text = rule_text
        self.action = action
        super().__init__(f"rule {rule_id!r} violated by action {action!r}: {rule_text}")


class SandboxDenied(HarnessError):
    """Raised by a SandboxBackend when policy denies an operation."""

    def __init__(self, reason: str, *, resource: str | None = None) -> None:
        self.reason = reason
        self.resource = resource
        super().__init__(f"sandbox denied: {reason}" + (f" (resource={resource})" if resource else ""))


class ApprovalDenied(HarnessError):
    """Raised when a requires_approval action is denied or times out."""

    def __init__(self, approval_id: str, decision: str, rationale: str | None = None) -> None:
        self.approval_id = approval_id
        self.decision = decision
        self.rationale = rationale
        super().__init__(f"approval {approval_id} {decision}: {rationale or ''}")


class BudgetExceeded(HarnessError):
    """Raised when a per-task cost budget is exceeded with hard_kill_at_budget=True."""


class RecursionBudgetExceeded(HarnessError):
    """Raised when subagent recursion exceeds the configured depth limit."""

    def __init__(self, depth: int, limit: int) -> None:
        self.depth = depth
        self.limit = limit
        super().__init__(f"subagent recursion depth {depth} exceeds limit {limit}")


class RedactionError(HarnessError):
    """Raised when a value that must be redacted would leak (e.g. MEMORY.md write)."""
