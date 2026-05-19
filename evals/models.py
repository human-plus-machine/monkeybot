"""Pydantic models for the evals HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Scenario(BaseModel):
    id: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    assertions: dict[str, Any] = Field(default_factory=dict)
    source: Literal["builtin", "operator"] = "builtin"


class TurnResult(BaseModel):
    input: str
    output: str
    trace_id: str | None = None
    # Per-turn metric scores when the metric API exposes them (optional).
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: dict[str, str] = Field(default_factory=dict)


class EvalRun(BaseModel):
    run_id: str
    scenario_id: str
    status: Literal["running", "completed", "failed"] = "running"
    turns: list[TurnResult] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    pass_rate: float | None = None
    created_at: str = ""
    error: str | None = None
    # Flat list of {metric, score, reason?, turn_index?} for UI expansion.
    score_details: list[dict[str, Any]] = Field(default_factory=list)


class CreateRunRequest(BaseModel):
    scenario_id: str | None = None
    scenario_ids: list[str] | None = None


class CreateRunResponse(BaseModel):
    run_id: str


class CreateScenarioBody(BaseModel):
    yaml: str = Field(..., description="Full scenario YAML document")


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "monkeybot-evals"
