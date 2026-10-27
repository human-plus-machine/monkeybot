"""Pass^N and Wilson intervals for one T5 trial run (§5.8).

Reliability scenarios must pass every trial. Capability scenarios (and anything
the class lists do not name) report a Wilson 95% interval. A missing baseline
is not a pass: the verdict is ``no baseline``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml

# The 95% normal quantile, to two decimals, so the bounds can be checked by hand.
Z_95 = 1.96
WARN_DROP = Fraction(1, 5)
SUITES_DIR = Path(__file__).resolve().parent / "suites"
_CLASSES = frozenset({"reliability", "capability"})


@dataclass(frozen=True)
class Interval:
    low: float
    high: float


@dataclass(frozen=True)
class Assignment:
    case: str | None
    klass: str


@dataclass(frozen=True)
class ClassLists:
    """Scenario id → class. A scenario in no list is capability with no case id."""

    by_scenario: dict[str, Assignment]
    drift_configs: tuple[str, ...]

    def assignment(self, scenario_id: str) -> Assignment:
        found = self.by_scenario.get(scenario_id)
        if found is None:
            return Assignment(case=None, klass="capability")
        return found

    def all_reliability(self, scenario_ids: list[str]) -> bool:
        return bool(scenario_ids) and all(
            self.assignment(scenario_id).klass == "reliability" for scenario_id in scenario_ids
        )


@dataclass(frozen=True)
class ScenarioVerdict:
    scenario_id: str
    case: str | None
    klass: str
    passed: int
    runs: int
    baseline_passed: int | None
    baseline_runs: int | None
    interval: Interval | None
    verdict: str

    @property
    def point(self) -> float:
        return self.passed / self.runs

    @property
    def baseline_rate(self) -> float | None:
        if self.baseline_passed is None or self.baseline_runs is None:
            return None
        return self.baseline_passed / self.baseline_runs


def wilson_interval(passed: int, runs: int, *, z: float = Z_95) -> Interval:
    """Wilson score interval for ``passed`` successes in ``runs`` trials.

    ``center = (p + z² / 2n) / (1 + z² / n)`` and
    ``margin = z · sqrt(p(1-p)/n + z² / 4n²) / (1 + z² / n)``, clamped to ``[0, 1]``.
    """
    if runs < 1:
        raise ValueError("runs must be positive")
    if not 0 <= passed <= runs:
        raise ValueError("passed must be between 0 and runs")
    n = float(runs)
    p = passed / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return Interval(low=max(0.0, center - margin), high=min(1.0, center + margin))


def judge(
    scenario_id: str,
    *,
    passed: int,
    runs: int,
    klass: str,
    case: str | None = None,
    baseline_passed: int | None = None,
    baseline_runs: int | None = None,
) -> ScenarioVerdict:
    """One scenario's verdict.

    Reliability fails when any trial fails (pass^N), with or without a baseline.
    A full pass still needs a baseline; otherwise the verdict is ``no baseline``.

    Capability fails when the baseline rate is strictly above the Wilson upper
    bound, and warns when the point estimate drops by at least 0.2. Fail wins
    when both apply. No baseline counts are ``no baseline``, not a pass.
    """
    if klass not in _CLASSES:
        raise ValueError(f"class must be reliability or capability, got {klass!r}")
    if runs < 1:
        raise ValueError("runs must be positive")
    if not 0 <= passed <= runs:
        raise ValueError("passed must be between 0 and runs")
    has_baseline = (
        baseline_passed is not None
        and baseline_runs is not None
        and baseline_runs > 0
        and 0 <= baseline_passed <= baseline_runs
    )
    if klass == "reliability":
        if passed < runs:
            verdict = "fail"
        elif not has_baseline:
            verdict = "no baseline"
        else:
            verdict = "pass"
        return ScenarioVerdict(
            scenario_id=scenario_id,
            case=case,
            klass=klass,
            passed=passed,
            runs=runs,
            baseline_passed=baseline_passed if has_baseline else None,
            baseline_runs=baseline_runs if has_baseline else None,
            interval=None,
            verdict=verdict,
        )

    interval = wilson_interval(passed, runs)
    if not has_baseline:
        verdict = "no baseline"
        kept_passed = None
        kept_runs = None
    else:
        assert baseline_passed is not None and baseline_runs is not None
        rate = baseline_passed / baseline_runs
        drop = Fraction(baseline_passed, baseline_runs) - Fraction(passed, runs)
        if rate > interval.high:
            verdict = "fail"
        elif drop >= WARN_DROP:
            verdict = "warn"
        else:
            verdict = "pass"
        kept_passed = baseline_passed
        kept_runs = baseline_runs
    return ScenarioVerdict(
        scenario_id=scenario_id,
        case=case,
        klass=klass,
        passed=passed,
        runs=runs,
        baseline_passed=kept_passed,
        baseline_runs=kept_runs,
        interval=interval,
        verdict=verdict,
    )


def default_trials(scenario_ids: list[str], classes: ClassLists) -> int:
    """§5.8: 8 trials, or 5 when every scenario in the suite is reliability."""
    if classes.all_reliability(scenario_ids):
        return 5
    return 8


def load_classes(directory: Path | None = None) -> ClassLists:
    """Read the three class lists. Drift scenarios are capability (at least k of N)."""
    root = directory or SUITES_DIR
    by_scenario: dict[str, Assignment] = {}
    drift_configs: tuple[str, ...] = ()
    for stem in ("reliability", "capability", "drift"):
        path = root / f"{stem}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must be a mapping")
        implied = "capability" if stem == "drift" else stem
        if stem == "drift":
            drift_configs = _configs(data.get("configs"), path)
        for item in data.get("cases") or []:
            _take_case(item, implied=implied, path=path, into=by_scenario)
    return ClassLists(by_scenario=by_scenario, drift_configs=drift_configs)


def _configs(raw: Any, path: Path) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise ValueError(f"{path} configs must be a non-empty list of strings")
    return tuple(raw)


def _take_case(
    item: Any,
    *,
    implied: str,
    path: Path,
    into: dict[str, Assignment],
) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"{path} cases must be mappings")
    case = item.get("case")
    if not isinstance(case, str) or not case.startswith("T5-"):
        raise ValueError(f"{path} case must be a T5 id, got {case!r}")
    stated = item.get("class", implied)
    if stated != implied:
        raise ValueError(f"{path} {case} class is {stated!r}, not {implied!r}")
    scenarios = item.get("scenarios")
    if (
        not isinstance(scenarios, list)
        or not scenarios
        or not all(isinstance(sid, str) and sid for sid in scenarios)
    ):
        raise ValueError(f"{path} {case} scenarios must be a non-empty list of ids")
    assignment = Assignment(case=case, klass=implied)
    for scenario_id in scenarios:
        previous = into.get(scenario_id)
        if previous is not None:
            raise ValueError(
                f"{scenario_id} is in {previous.case} and {case}; a scenario has one class"
            )
        into[scenario_id] = assignment
