"""Pydantic models shared by the eval runner, scorer, and report CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from regression.schema.steps import Step

_VERIFIER_BLOCKS = frozenset({"verifier_on", "verifier_off"})


class Scenario(BaseModel):
    id: str
    # Catalog case id (``T4-FILE-01``). P4-03 fills it in; failure-ledger records carry it.
    case: str | None = None
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
    # Directory under ``regression/fixtures/workspaces/`` copied into the agent workspace before the run (coding scenarios).
    # Messages may reference the copy as ``{workspace_subdir}``.
    workspace_fixture: str | None = None
    # Directories under ``regression/fixtures/workspaces/`` copied over the fixture, in order (e.g. a seeded bug).
    workspace_overlays: list[str] = Field(default_factory=list)
    # The scenario's ``config:``: agent-config overrides (dotted or nested keys) plus ``env``
    # for the gateway process. The runner boots a gateway with it (regression/t4/gateway.py).
    config: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)

    def message_groups(self) -> list[list[str]]:
        """Resolve ``sessions`` (if set) or wrap ``messages`` as a single session."""
        if self.sessions:
            return self.sessions
        return [self.messages]

    def has_verifier_blocks(self) -> bool:
        return bool(_VERIFIER_BLOCKS & set(self.assertions))


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
    path_args: list[str] = Field(default_factory=list)
    error: str | None = None
    call_id: str = ""
    # Outcome once the result arrives: False on a tool error or a JSON result with ``ok: false``
    # (e.g. a failing ``run_command``); None while the call is still unresolved.
    ok: bool | None = None
    # Untruncated arguments and result text; ``args_summary`` stays the 300-char display form.
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""


class VerdictRecord(BaseModel):
    status: str = ""
    severity: str = ""
    triggering_signals: list[str] = Field(default_factory=list)


class TurnResult(BaseModel):
    input: str
    output: str
    trace_id: str | None = None
    usage: UsageSummary = Field(default_factory=UsageSummary)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    summarizations_count: int = 0
    verdicts: list[VerdictRecord] = Field(default_factory=list)
    # ``TurnComplete`` events seen for this turn's request id (T4-GW-01: exactly one).
    turn_completes: int = 0
    session_id: str = ""
    request_id: str = ""
    # 1-based message group this turn belongs to; 0 for a scheduler tick (``invoke_tick``).
    session_index: int = 0
    # Every event for this request as received, and the same frames with their SSE ``id``.
    events: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    frames: list[dict[str, Any]] = Field(default_factory=list, exclude=True)


class EvalRun(BaseModel):
    """One scenario's turns + judge scores, aggregated for assertion checks."""

    run_id: str
    scenario_id: str
    turns: list[TurnResult] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    # Per-run copy of the scenario's ``workspace_fixture`` that the agent worked in.
    workspace_dir: Path | None = None
    # The agent's workspace root; session transcripts live under ``.monkeybot/transcripts``.
    agent_workspace: Path | None = None
    session_ids: list[str] = Field(default_factory=list)
    # One snapshot per ``read_history`` step: the chat-history API's wire rows.
    history_snapshots: list[list[dict[str, Any]]] = Field(default_factory=list)
    # One system prompt per turn, aligned with ``turns`` (empty when none was recorded).
    system_prompts: list[str] = Field(default_factory=list)
    # Last ``SessionManifest`` of each child transcript started by this run.
    manifests: list[dict[str, Any]] = Field(default_factory=list)

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

    def verdicts_count(self) -> int:
        return sum(len(t.verdicts) for t in self.turns)

    def verdicts(self) -> list[VerdictRecord]:
        return [v for t in self.turns for v in t.verdicts]

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


class ScenarioRun(list[TurnResult]):
    """The turns of one live scenario, plus what its steps recorded.

    A list so existing callers can index turns. History, prompts, and child
    manifests hang off the list because they are not turns.
    """

    def __init__(self, turns: list[TurnResult] | None = None) -> None:
        super().__init__(turns or [])
        self.history_snapshots: list[list[dict[str, Any]]] = []
        self.system_prompts: list[str] = []
        self.manifests: list[dict[str, Any]] = []
