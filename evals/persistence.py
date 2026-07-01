"""Disk persistence for eval suite runs and baselines.

One JSON file per run under ``evals/runs/`` (gitignored). Baselines are the same shape
minus full turn text, committed under ``evals/baselines/``. ``schema_version`` lets the
report command evolve fields without breaking old baselines.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

RUNS_DIR = Path(__file__).resolve().parent / "runs"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"


class ScenarioAggregate(BaseModel):
    scenario_id: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    status: str = "errored"  # passed | failed | errored
    messages_count: int = 0
    quality_scores: dict[str, float] = Field(default_factory=dict)
    quality_reasons: dict[str, str] = Field(default_factory=dict)
    pass_rate: float | None = None
    latency_ms_total: int = 0
    latency_ms_by_turn: list[int] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls_count: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    tool_errors_count: int = 0
    llm_calls_count: int = 0  # not exposed on SSE yet; stays 0 until a stream event carries it
    summarizations_count: int = 0
    subagent_calls_count: int = 0
    trace_ids: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    requirement_failures: list[str] = Field(default_factory=list)


class SuiteAggregate(BaseModel):
    scenario_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    errored_count: int = 0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    mean_quality_by_metric: dict[str, float] = Field(default_factory=dict)
    worst_regressed_scenarios: list[str] = Field(default_factory=list)
    best_improved_scenarios: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RunRecord(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    suite: str
    git_sha: str = ""
    git_branch: str = ""
    dirty_worktree: bool = False
    started_at: str = ""
    finished_at: str = ""
    agent_url: str = ""
    model_provider: str = ""
    model_name: str = ""
    judge_provider: str | None = None
    judge_model: str | None = None
    scenarios: list[ScenarioAggregate] = Field(default_factory=list)
    suite_aggregate: SuiteAggregate = Field(default_factory=SuiteAggregate)


class BaselineFile(BaseModel):
    schema_version: int = SCHEMA_VERSION
    recorded_at: str = ""
    git_sha: str = ""
    suite: str = ""
    suite_aggregate: SuiteAggregate = Field(default_factory=SuiteAggregate)
    scenarios: list[ScenarioAggregate] = Field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_suite_aggregate(scenarios: list[ScenarioAggregate]) -> SuiteAggregate:
    passed = sum(1 for s in scenarios if s.status == "passed")
    failed = sum(1 for s in scenarios if s.status == "failed")
    errored = sum(1 for s in scenarios if s.status == "errored")
    latencies = sorted(s.latency_ms_total for s in scenarios)
    mean_latency = sum(latencies) / len(latencies) if latencies else 0.0
    p95_latency = latencies[int(0.95 * (len(latencies) - 1))] if latencies else 0.0
    total_tokens = sum(s.input_tokens + s.output_tokens for s in scenarios)
    total_cost = sum(s.cost_usd for s in scenarios)

    metric_totals: dict[str, list[float]] = {}
    for s in scenarios:
        for metric, score in s.quality_scores.items():
            metric_totals.setdefault(metric, []).append(score)
    mean_quality = {m: sum(v) / len(v) for m, v in metric_totals.items()}

    return SuiteAggregate(
        scenario_count=len(scenarios),
        passed_count=passed,
        failed_count=failed,
        errored_count=errored,
        mean_latency_ms=mean_latency,
        p95_latency_ms=float(p95_latency),
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        mean_quality_by_metric=mean_quality,
    )


def save_run(record: RunRecord, runs_dir: Path = RUNS_DIR) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record.run_id}.json"
    path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return path


def latest_run(runs_dir: Path = RUNS_DIR) -> Path | None:
    if not runs_dir.is_dir():
        return None
    candidates = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def load_run(path: Path) -> RunRecord:
    return RunRecord.model_validate(json.loads(path.read_text()))


def load_baseline(path: Path) -> BaselineFile | None:
    if not path.is_file():
        return None
    return BaselineFile.model_validate(json.loads(path.read_text()))


def save_baseline(baseline: BaselineFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def baseline_from_run(record: RunRecord) -> BaselineFile:
    return BaselineFile(
        recorded_at=utc_now_iso(),
        git_sha=record.git_sha,
        suite=record.suite,
        suite_aggregate=record.suite_aggregate,
        scenarios=record.scenarios,
    )


def git_metadata(repo_root: Path | None = None) -> dict[str, Any]:
    """Best-effort ``git_sha`` / ``git_branch`` / ``dirty_worktree`` for the run record."""
    import subprocess

    cwd = str(repo_root) if repo_root else None

    def _run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True
            )
            return out.stdout.strip()
        except Exception:
            return ""

    sha = _run("rev-parse", "HEAD")
    branch = _run("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_run("status", "--porcelain"))
    return {"git_sha": sha, "git_branch": branch, "dirty_worktree": dirty}
