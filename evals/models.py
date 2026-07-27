"""Pydantic models shared by the eval runner, scorer, and report CLI."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Scenario(BaseModel):
    id: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    # Single-session convenience form: one session, one message per turn.
    messages: list[str] = Field(default_factory=list)
    # Multi-session form: each inner list is a batch of messages sent in its own session,
    # sessions opened sequentially against the same running agent (same workspace/memory).
    # When set, takes precedence over ``messages``.
    sessions: list[list[str]] | None = None
    # Pause between sessions (not before the first) so the debounced background memory
    # organizer (POST_TURN hook) has time to promote writes before the next session probes.
    session_pause_sec: float = 5.0
    assertions: dict[str, Any] = Field(default_factory=dict)

    def message_groups(self) -> list[list[str]]:
        """Resolve ``sessions`` (if set) or wrap ``messages`` as a single session."""
        if self.sessions:
            return self.sessions
        return [self.messages]


class UsageSummary(BaseModel):
    """Token/cost/duration rollup, summed from ``TurnComplete.usage`` across turns."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


class ToolCallRecord(BaseModel):
    tool: str
    args_summary: str = ""
    error: str | None = None


class TurnResult(BaseModel):
    input: str
    output: str
    trace_id: str | None = None
    usage: UsageSummary = Field(default_factory=UsageSummary)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    summarizations_count: int = 0


class EvalRun(BaseModel):
    """One scenario's turns + judge scores, aggregated for assertion checks."""

    run_id: str
    scenario_id: str
    turns: list[TurnResult] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)

    def tool_calls_count(self) -> int:
        return sum(len(t.tool_calls) for t in self.turns)

    def tool_calls_by_name(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.turns:
            for c in t.tool_calls:
                counts[c.tool] = counts.get(c.tool, 0) + 1
        return counts

    def tool_errors_count(self) -> int:
        """Count tool errors the agent never recovered from.

        An error is "self-corrected" (not counted) if a later call to the same tool
        in this run succeeded — e.g. a bad-path read_file followed by a good one.
        Matching is by tool name only, not args: any later success on the same tool
        forgives *all* prior errors on it, even against different paths/args (e.g. a
        failed write_file("a") then a successful write_file("b") counts as resolved).
        Fine for the current scenarios' single-error cases; tighten to per-args
        matching if a scenario ever needs to tell those apart.
        """
        calls = [c for t in self.turns for c in t.tool_calls]
        unresolved = 0
        for i, c in enumerate(calls):
            if not c.error:
                continue
            if any(not later.error for later in calls[i + 1 :] if later.tool == c.tool):
                continue
            unresolved += 1
        return unresolved

    def subagent_calls_count(self) -> int:
        return self.tool_calls_by_name().get("task", 0)

    def summarizations_count(self) -> int:
        return sum(t.summarizations_count for t in self.turns)

    def usage_total(self) -> UsageSummary:
        total = UsageSummary()
        for t in self.turns:
            u = t.usage
            total.input_tokens += u.input_tokens
            total.output_tokens += u.output_tokens
            total.cached_tokens += u.cached_tokens
            total.cache_read_tokens += u.cache_read_tokens
            total.cache_creation_tokens += u.cache_creation_tokens
            total.cost_usd += u.cost_usd
            total.duration_ms += u.duration_ms
        return total
