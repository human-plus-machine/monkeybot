"""RunPackage schema v1 — durable, versioned record of a complete agent run.

Designed so GRPO-style consumers can ingest it without changes:
 - Has run_id / session_id / principal / versions
 - Has token_trace, tool_calls, context_events, approvals, eval_scores
 - ``outcome`` is a first-class enum
 - ``replay()`` can re-drive the original inputs through a fake LLM for reproducibility
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .events import HarnessEvent, Principal, VersionTriple


class TokenAccounting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float | None = None
    latency_ms: int


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: str
    requested_at: datetime
    decided_at: datetime | None = None
    decided_by: Principal | None = None
    decision: Literal["approved", "denied", "timeout"]
    rationale: str | None = None
    intended_action: str
    blast_radius: str
    rollback_plan: str
    confidence: float | None = None


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: str
    name: str
    source: Literal["native", "skill", "mcp", "subagent", "sandbox_exec"]
    args_redacted: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    tier: Literal["preapproved", "requires_approval", "denied"] = "preapproved"
    approval: ApprovalRecord | None = None
    latency_ms: int = 0
    success: bool = True


class RunPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    run_id: str
    session_id: str
    principal: Principal
    versions: VersionTriple
    started_at: datetime
    ended_at: datetime
    inputs: list[dict] = Field(default_factory=list)
    outputs: list[dict] = Field(default_factory=list)
    token_trace: list[TokenAccounting] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    subagent_runs: list["RunPackage"] = Field(default_factory=list)
    context_events: list[HarnessEvent] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    eval_scores: dict[str, float] = Field(default_factory=dict)
    outcome: Literal["pass", "fail", "pass-with-warnings", "escalated"] = "pass"
    extensions: dict[str, Any] = Field(default_factory=dict)

    def replay(self, fake_llm: Any) -> list[Any]:
        """Re-emit stored inputs against a fake LLM for deterministic tests.

        Returns the raw outputs produced. ``fake_llm`` must expose an ``invoke``
        method accepting a list of dict messages.
        """
        outs: list[Any] = []
        for msg in self.inputs:
            outs.append(fake_llm.invoke([msg]))
        return outs


RunPackage.model_rebuild()


class RunPackageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    session_id: str
    principal_id: str
    uri: str
    created_at: datetime
