"""HarnessEvent schema v1 — the event types flowing on the EventBus.

Frozen Pydantic models. Consumers write handlers against these to wire Phoenix,
DeepEval, OTel, or custom sinks without modifying the framework.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    AGENT_START = "agent.start"
    AGENT_END = "agent.end"
    LLM_CALL = "llm.call"
    LLM_RESULT = "llm.result"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    SUBAGENT_SPAWN = "subagent.spawn"
    SUBAGENT_RETURN = "subagent.return"
    CONTEXT_SUMMARIZE = "context.summarize"
    CONTEXT_RESET = "context.hard_reset"
    CONTEXT_OFFLOAD = "context.offload"
    RULE_VETO = "rule.veto"
    APPROVAL_REQUEST = "approval.requested"
    APPROVAL_DECISION = "approval.decision"
    SANDBOX_EXEC = "sandbox.exec"
    SANDBOX_DENIED = "sandbox.denied"
    CHECKPOINT_WRITE = "checkpoint.write"
    CHECKPOINT_RESTORE = "checkpoint.restore"
    TASK_COMPLETE = "task.complete"
    TASK_FAILED = "task.failed"
    BUDGET_UTILIZATION = "budget.utilization"
    ERROR = "error"
    # BEGIN harness-extensibility story 5
    IDENTITY_LOAD = "identity.load"
    IDENTITY_LOAD_FAILED = "identity.load_failed"
    IDENTITY_CACHE_EVICT = "identity.cache_evict"
    IDENTITY_BUST = "identity.bust"
    # END harness-extensibility story 5
    # BEGIN harness-extensibility story 6
    SECRET_RESOLVED = "secret.resolved"
    # END harness-extensibility story 6


class VersionTriple(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    harness: str
    deep_agents: str
    model: str


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["user", "service", "anonymous"] = "anonymous"
    id: str = "anonymous"
    email_hash: str | None = None
    tenant: str | None = None


class HarnessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    session_id: str
    parent_run_id: str | None = None
    principal: Principal
    versions: VersionTriple
    ts: datetime
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = False
