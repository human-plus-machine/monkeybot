"""T5 statistics and baseline comparison, with no live trial and no real baseline write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from regression.t5.compare import (
    baseline_document,
    baseline_filename,
    baseline_path,
    lookup,
    scenario_counts,
    write_baseline,
)
from regression.t5.stats import default_trials, judge, load_classes, wilson_interval
from regression.t5.trials import (
    _config_slug,
    _summarize,
    compare,
    exit_status,
    judge_summary,
    publish,
    record_verdicts,
)
from testkit.failure_ledger import LedgerWriter

# Wilson score interval at z = 1.96, computed independently of stats.wilson_interval:
#   denom = 1 + z²/n
#   center = (p + z²/(2n)) / denom
#   margin = z * sqrt(p(1-p)/n + z²/(4n²)) / denom
# 4/8 → 0.215212526824 .. 0.784787473176
# 6/8 → 0.409269879103 .. 0.928522311142
# 2/8 → 0.071477688858 .. 0.590730120897
# 6/10 → 0.312669547450 .. 0.831822418796
_W4_8 = (0.215212526824, 0.784787473176)
_W6_8 = (0.409269879103, 0.928522311142)
_W2_8 = (0.071477688858, 0.590730120897)
_W6_10 = (0.312669547450, 0.831822418796)

_ROOT = Path(__file__).resolve().parents[2]
_SCENARIOS = _ROOT / "regression" / "t4" / "scenarios"


def test_wilson_matches_hand_computed_bounds() -> None:
    four = wilson_interval(4, 8)
    assert four.low == pytest.approx(_W4_8[0], abs=1e-12)
    assert four.high == pytest.approx(_W4_8[1], abs=1e-12)
    six = wilson_interval(6, 8)
    assert (six.low, six.high) == pytest.approx(_W6_8, abs=1e-12)
    two = wilson_interval(2, 8)
    assert (two.low, two.high) == pytest.approx(_W2_8, abs=1e-12)
    six_of_ten = wilson_interval(6, 10)
    assert (six_of_ten.low, six_of_ten.high) == pytest.approx(_W6_10, abs=1e-12)
    none = wilson_interval(0, 8)
    assert none.low == 0.0
    assert none.high == pytest.approx(0.324415619511, abs=1e-12)
    all_passed = wilson_interval(8, 8)
    assert all_passed.high == 1.0
    assert all_passed.low == pytest.approx(0.675584380489, abs=1e-12)


def test_capability_fail_warn_and_pass_rules() -> None:
    failed = judge(
        "coding_hard/fix_discount_rounding",
        passed=2,
        runs=8,
        klass="capability",
        baseline_passed=8,
        baseline_runs=8,
    )
    assert failed.verdict == "fail"
    assert failed.interval is not None
    assert failed.interval.high == pytest.approx(_W2_8[1], abs=1e-12)
    assert failed.interval.high < 1.0

    warned = judge(
        "context/post_compaction_continuity",
        passed=4,
        runs=8,
        klass="capability",
        baseline_passed=6,
        baseline_runs=8,
    )
    assert warned.verdict == "warn"
    assert warned.interval is not None
    assert warned.interval.high >= (6 / 8)
    assert (6 / 8) - (4 / 8) >= 0.2

    exact = judge(
        "memory/recall_single_session",
        passed=6,
        runs=10,
        klass="capability",
        baseline_passed=8,
        baseline_runs=10,
    )
    assert exact.verdict == "warn"
    assert exact.interval is not None
    assert exact.interval.high == pytest.approx(_W6_10[1], abs=1e-12)
    assert exact.interval.high >= 0.8

    passed = judge(
        "coding/fix_failing_test",
        passed=6,
        runs=8,
        klass="capability",
        baseline_passed=7,
        baseline_runs=8,
    )
    assert passed.verdict == "pass"
    assert passed.interval is not None
    assert passed.interval.high >= (7 / 8)
    assert (7 / 8) - (6 / 8) < 0.2


def test_reliability_is_pass_to_the_n() -> None:
    assert (
        judge(
            "skills/skill_invocation",
            passed=5,
            runs=5,
            klass="reliability",
            baseline_passed=5,
            baseline_runs=5,
        ).verdict
        == "pass"
    )
    missed = judge(
        "multi_turn/task_tracking",
        passed=4,
        runs=5,
        klass="reliability",
        baseline_passed=5,
        baseline_runs=5,
    )
    assert missed.verdict == "fail"
    assert missed.baseline_passed == 5
    assert missed.baseline_runs == 5
    still_fail = judge(
        "skills/skill_invocation",
        passed=4,
        runs=5,
        klass="reliability",
    )
    assert still_fail.verdict == "fail"
    assert still_fail.baseline_passed is None


def test_missing_baseline_is_not_a_pass() -> None:
    capability = judge(
        "coding/navigate_repo",
        passed=8,
        runs=8,
        klass="capability",
    )
    reliability = judge(
        "subagents/dispatch_complete",
        passed=5,
        runs=5,
        klass="reliability",
    )
    assert capability.verdict == "no baseline"
    assert reliability.verdict == "no baseline"
    assert capability.interval is not None
    assert reliability.interval is None


def test_class_lists_name_real_scenarios_and_leave_the_rest_as_capability() -> None:
    classes = load_classes()
    assert classes.drift_configs == ("verifier-tracker", "verifier-judge")
    expected = {
        "skills/skill_invocation": ("T5-SKILL-01", "reliability"),
        "multi_turn/task_tracking": ("T5-TODO-01", "reliability"),
        "subagents/dispatch_complete": ("T5-SUB-01", "reliability"),
        "subagents/persona_dispatch": ("T5-SUB-01", "reliability"),
        "context/post_compaction_continuity": ("T5-CTX-01", "capability"),
        "scheduler/model_starts_loop": ("T5-SCHED-01", "capability"),
        "mcp/enable_then_echo_live": ("T5-MCP-01", "capability"),
        "memory/recall_single_session": ("T5-MEM-01", "capability"),
        "memory/recall_cross_session": ("T5-MEM-01", "capability"),
        "memory/search_past_session": ("T5-MEM-01", "capability"),
        "drift/path_constraint": ("T5-VER-01", "capability"),
        "drift/supersession": ("T5-VER-01", "capability"),
        "drift/preempt": ("T5-VER-01", "capability"),
        "drift/declares_done": ("T5-VER-01", "capability"),
        "drift/repeat_correction": ("T5-VER-01", "capability"),
    }
    for scenario_id in (
        "coding/fix_failing_test",
        "coding/add_feature_with_tests",
        "coding/navigate_repo",
        "coding_hard/fix_discount_rounding",
        "coding_hard/add_bulk_discount",
        "coding_hard/noisy_test_failure",
        "coding_hard/navigate_intl_rate",
        "coding_hard/rename_calc_tax",
        "coding_hard/honest_unfixable_failure",
        "coding_hard/multi_feature_long",
    ):
        expected[scenario_id] = ("T5-CODE-01", "capability")
    assert {item.case for item in classes.by_scenario.values()} == {
        pair[0] for pair in expected.values()
    }
    for scenario_id, (case, klass) in expected.items():
        assignment = classes.assignment(scenario_id)
        assert (assignment.case, assignment.klass) == (case, klass)
        assert (_SCENARIOS / f"{scenario_id}.yaml").is_file()
    for scenario_id in (
        "context/summarization_trigger",
        "scheduler/http_one_tick_live",
        "mcp/tool_invoke",
        "file/core_read",
    ):
        assignment = classes.assignment(scenario_id)
        assert assignment.case is None
        assert assignment.klass == "capability"
    assert "T5-GW-01" not in {item.case for item in classes.by_scenario.values()}
    assert "T5-GW-02" not in {item.case for item in classes.by_scenario.values()}
    assert (
        default_trials(["subagents/dispatch_complete", "subagents/persona_dispatch"], classes) == 5
    )
    assert default_trials(["coding/fix_failing_test"], classes) == 8
    assert default_trials(["skills/skill_invocation", "file/core_read"], classes) == 8


def test_baseline_key_ignores_a_different_provider_or_model(tmp_path: Path) -> None:
    assert (
        baseline_filename("coding_hard", "ollama-cloud", "glm-5.3-flash")
        == "coding_hard__ollama-cloud__glm-5.3-flash.json"
    )
    assert (
        baseline_filename("drift", "ollama-cloud", "glm-5.3-flash", "verifier-judge")
        == "drift@verifier-judge__ollama-cloud__glm-5.3-flash.json"
    )
    assert _config_slug("monkeybot.verifier-judge.yaml") == "verifier-judge"
    assert _config_slug("verifier-tracker") == "verifier-tracker"
    other = baseline_path("coding_hard", "other-provider", "glm-5.3-flash", directory=tmp_path)
    write_baseline(
        other,
        baseline_document(
            suite="coding_hard",
            provider="other-provider",
            model="glm-5.3-flash",
            config=None,
            trials=8,
            scenarios={"coding/fix_failing_test": (8, 8)},
        ),
    )
    assert lookup("coding_hard", "ollama-cloud", "glm-5.3-flash", directory=tmp_path) is None
    assert not baseline_path(
        "coding_hard", "ollama-cloud", "glm-5.3-flash", directory=tmp_path
    ).exists()


def test_regressed_baseline_records_trial_and_baseline_counts(tmp_path: Path) -> None:
    scenario_id = "coding_hard/fix_discount_rounding"
    path = baseline_path("coding_hard", "ollama-cloud", "glm-5.3-flash", directory=tmp_path)
    document = baseline_document(
        suite="coding_hard",
        provider="ollama-cloud",
        model="glm-5.3-flash",
        config=None,
        trials=8,
        scenarios={scenario_id: (8, 8)},
    )
    assert lookup("coding_hard", "ollama-cloud", "glm-5.3-flash", directory=tmp_path) is None
    write_baseline(path, document)
    loaded = lookup("coding_hard", "ollama-cloud", "glm-5.3-flash", directory=tmp_path)
    assert scenario_counts(loaded, scenario_id) == (8, 8)
    snapshot = path.read_bytes()

    summary = _counts_summary({scenario_id: (2, 8)})
    verdicts = judge_summary(summary, baseline=loaded, classes=load_classes())
    assert [item.verdict for item in verdicts] == ["fail"]
    assert verdicts[0].case == "T5-CODE-01"
    ledger = _ledger(tmp_path / "ledger")
    written = publish(
        summary,
        verdicts,
        suite="coding_hard",
        provider="ollama-cloud",
        model="glm-5.3-flash",
        config=None,
        trials=8,
        update_baseline=False,
        directory=tmp_path,
        ledger=ledger,
        agent_url="http://127.0.0.1:8787",
    )
    assert written is None
    assert path.read_bytes() == snapshot
    ledger.finish(exit_status=1, counts=_counts(failed=1), outcomes=[])
    failure = _only_failure(ledger.path)
    assert failure["severity"] == "fail"
    assert failure["tier"] == "t5"
    assert failure["case"] == "T5-CODE-01"
    assert failure["trials"]["k"] == 2
    assert failure["trials"]["n"] == 8
    assert failure["trials"]["baseline_k"] == 8
    assert failure["trials"]["baseline_n"] == 8
    assert failure["trials"]["trial"] == "2/8"
    assert failure["trials"]["baseline"] == "8/8"
    assert "trial 2/8 baseline 8/8" in failure["message"]


def test_warn_records_severity_warn_and_does_not_write_the_baseline(tmp_path: Path) -> None:
    scenario_id = "context/post_compaction_continuity"
    path = baseline_path("context", "ollama-cloud", "glm-5.3-flash", directory=tmp_path)
    write_baseline(
        path,
        baseline_document(
            suite="context",
            provider="ollama-cloud",
            model="glm-5.3-flash",
            config=None,
            trials=8,
            scenarios={scenario_id: (6, 8)},
        ),
    )
    snapshot = path.read_bytes()
    summary = _counts_summary({scenario_id: (4, 8)})
    verdicts = judge_summary(
        summary,
        baseline=lookup("context", "ollama-cloud", "glm-5.3-flash", directory=tmp_path),
        classes=load_classes(),
    )
    assert [item.verdict for item in verdicts] == ["warn"]
    ledger = _ledger(tmp_path / "ledger")
    assert (
        publish(
            summary,
            verdicts,
            suite="context",
            provider="ollama-cloud",
            model="glm-5.3-flash",
            config=None,
            trials=8,
            update_baseline=False,
            directory=tmp_path,
            ledger=ledger,
        )
        is None
    )
    assert path.read_bytes() == snapshot
    ledger.finish(exit_status=0, counts=_counts(passed=1), outcomes=[])
    failure = _only_failure(ledger.path)
    assert failure["severity"] == "warn"
    assert failure["trials"]["trial"] == "4/8"
    assert failure["trials"]["baseline"] == "6/8"
    assert "trial 4/8 baseline 6/8" in failure["message"]
    assert exit_status(verdicts) == 0


def test_no_baseline_writes_nothing_and_is_not_a_pass(tmp_path: Path) -> None:
    scenario_id = "skills/skill_invocation"
    summary = _counts_summary({scenario_id: (5, 5)})
    verdicts = judge_summary(summary, baseline=None, classes=load_classes())
    assert [item.verdict for item in verdicts] == ["no baseline"]
    ledger = _ledger(tmp_path / "ledger")
    assert (
        publish(
            summary,
            verdicts,
            suite="smoke",
            provider="ollama-cloud",
            model="glm-5.3-flash",
            config=None,
            trials=5,
            update_baseline=False,
            directory=tmp_path,
            ledger=ledger,
        )
        is None
    )
    assert list(tmp_path.glob("*.json")) == []
    ledger.finish(exit_status=2, counts=_counts(), outcomes=[])
    assert ledger.failures == []
    assert exit_status(verdicts) == 2
    assert (
        exit_status(
            [
                judge(
                    "multi_turn/task_tracking",
                    passed=3,
                    runs=5,
                    klass="reliability",
                    baseline_passed=5,
                    baseline_runs=5,
                )
            ]
        )
        == 1
    )


def test_update_baseline_is_the_only_writer(tmp_path: Path) -> None:
    scenario_id = "mcp/enable_then_echo_live"
    summary = _counts_summary({scenario_id: (7, 8)})
    verdicts = judge_summary(summary, baseline=None, classes=load_classes())
    ledger = _ledger(tmp_path / "ledger")
    written = publish(
        summary,
        verdicts,
        suite="mcp",
        provider="ollama-cloud",
        model="glm-5.3-flash",
        config=None,
        trials=8,
        update_baseline=True,
        directory=tmp_path,
        ledger=ledger,
    )
    assert written is not None
    assert written.name == "mcp__ollama-cloud__glm-5.3-flash.json"
    saved = json.loads(written.read_text(encoding="utf-8"))
    assert saved["scenarios"][scenario_id] == {"passed": 7, "runs": 8}
    assert ledger.failures == []
    snapshot = written.read_bytes()
    again = lookup("mcp", "ollama-cloud", "glm-5.3-flash", directory=tmp_path)
    assert scenario_counts(again, scenario_id) == (7, 8)
    assert written.read_bytes() == snapshot
    record_verdicts(
        verdicts,
        ledger,
        suite="mcp",
        provider="ollama-cloud",
        model="glm-5.3-flash",
        config=None,
        agent_url="http://127.0.0.1:8787",
    )
    assert ledger.failures == []
    assert written.read_bytes() == snapshot


def test_mempalace_call_rate_is_in_the_summary() -> None:
    summary = _summarize(
        "memory",
        "memory",
        [
            [_trial_row("memory/search_past_session", mempalace_calls=1)],
            [_trial_row("memory/search_past_session", mempalace_calls=0)],
        ],
    )
    row = summary["scenarios"]["memory/search_past_session"]
    assert row["mempalace_call_rate"] == 0.5
    assert row["mean_mempalace_calls"] == 0.5
    assert row["passed"] == 2
    assert row["runs"] == 2


def test_label_compare_still_reports_passed_over_runs(tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(
        json.dumps(_label_doc("branch", {"coding/fix_failing_test": (4, 5)})),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(_label_doc("main", {"coding/fix_failing_test": (5, 5)})),
        encoding="utf-8",
    )
    table = compare([str(first), str(second)])
    assert "| coding/fix_failing_test | 4/5 | 5/5 |" in table
    assert "**4/5**" in table
    assert "**5/5**" in table


def _counts_summary(counts: dict[str, tuple[int, int]]) -> dict[str, object]:
    scenarios = {}
    for scenario_id, (passed, runs) in counts.items():
        scenarios[scenario_id] = {
            "runs": runs,
            "passed": passed,
            "pass_rate": passed / runs,
            "failures": [],
        }
    return {"label": "run", "suite": "synthetic", "trials": 1, "scenarios": scenarios}


def _trial_row(scenario_id: str, *, mempalace_calls: int) -> dict[str, object]:
    return {
        "scenario_id": scenario_id,
        "status": "passed",
        "failure": "",
        "tool_calls_count": mempalace_calls,
        "tool_calls_by_name": {},
        "mempalace_calls": mempalace_calls,
        "tool_errors_count": 0,
        "input_tokens": 1,
        "output_tokens": 1,
    }


def _label_doc(label: str, counts: dict[str, tuple[int, int]]) -> dict[str, object]:
    scenarios = {}
    passed_total = 0
    runs_total = 0
    for scenario_id, (passed, runs) in counts.items():
        scenarios[scenario_id] = {
            "passed": passed,
            "runs": runs,
            "mean_tokens": 10,
        }
        passed_total += passed
        runs_total += runs
    return {
        "label": label,
        "overall_pass_rate": passed_total / runs_total,
        "scenarios": scenarios,
    }


def _ledger(directory: Path) -> LedgerWriter:
    ledger = LedgerWriter(directory)
    ledger.start(argv=["python", "-m", "regression.t5", "--suite", "synthetic"])
    return ledger


def _counts(*, passed: int = 0, failed: int = 0) -> dict[str, int]:
    return {
        "passed": passed,
        "failed": failed,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed_strict": 0,
    }


def _only_failure(path: Path | None) -> dict[str, object]:
    assert path is not None
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    failures = [row for row in rows if row["kind"] == "failure"]
    assert len(failures) == 1
    return failures[0]
