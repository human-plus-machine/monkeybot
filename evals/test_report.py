"""Assert-based self-check for evals/report.py gate logic + evals/persistence.py aggregation.

Run: python evals/test_report.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from persistence import (  # noqa: E402
    BaselineFile,
    RunRecord,
    ScenarioAggregate,
    build_suite_aggregate,
    save_run,
)
from report import compare_to_baseline, main  # noqa: E402


def _agg(**kwargs) -> ScenarioAggregate:
    base = {
        "scenario_id": "s",
        "status": "passed",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.01,
    }
    base.update(kwargs)
    return ScenarioAggregate(**base)


def _record(scenarios: list[ScenarioAggregate]) -> RunRecord:
    return RunRecord(
        run_id="r",
        suite="smoke",
        scenarios=scenarios,
        suite_aggregate=build_suite_aggregate(scenarios),
    )


def _baseline(scenarios: list[ScenarioAggregate]) -> BaselineFile:
    return BaselineFile(
        suite="smoke", scenarios=scenarios, suite_aggregate=build_suite_aggregate(scenarios)
    )


def test_no_baseline_only_checks_current_run() -> None:
    current = _record(
        [_agg(status="passed"), _agg(scenario_id="t", status="failed", requirement_failures=["x"])]
    )
    hard, warn = compare_to_baseline(None, current)
    assert len(hard) == 1
    assert "t" in hard[0]
    assert warn == []


def test_missing_baseline_is_noop_without_require_baseline() -> None:
    current = _record([_agg(status="passed")])
    hard, warn = compare_to_baseline(None, current)
    assert hard == []
    assert warn == []


def test_missing_baseline_hard_fails_when_required() -> None:
    current = _record([_agg(status="passed")])
    hard, _ = compare_to_baseline(None, current, require_baseline=True)
    assert len(hard) == 1
    assert "baseline" in hard[0].lower()


def test_scenario_errored_is_hard_fail() -> None:
    current = _record([_agg(status="errored", failure_reason="boom")])
    hard, _ = compare_to_baseline(None, current)
    assert len(hard) == 1
    assert "boom" in hard[0]


def test_regression_from_passed_to_failed_vs_baseline() -> None:
    baseline = _baseline([_agg(status="passed")])
    current = _record([_agg(status="failed", requirement_failures=["nope"])])
    hard, _ = compare_to_baseline(baseline, current)
    assert any("passed on baseline" in h for h in hard)


def test_token_regression_over_15pct_hard_fails() -> None:
    baseline = _baseline([_agg(input_tokens=1000, output_tokens=500)])
    current = _record([_agg(input_tokens=1300, output_tokens=500)])  # total +20%
    hard, _ = compare_to_baseline(baseline, current)
    assert any("total tokens" in h for h in hard)


def test_token_regression_under_15pct_does_not_fail() -> None:
    baseline = _baseline([_agg(input_tokens=1000, output_tokens=500)])
    current = _record([_agg(input_tokens=1050, output_tokens=500)])  # total +3.3%
    hard, _ = compare_to_baseline(baseline, current)
    assert hard == []


def test_quality_drop_is_warn_not_hard_fail() -> None:
    baseline = _baseline([_agg(quality_scores={"turn_relevancy": 0.9})])
    current = _record([_agg(quality_scores={"turn_relevancy": 0.8})])
    hard, warn = compare_to_baseline(baseline, current)
    assert hard == []
    assert any("turn_relevancy" in w for w in warn)


def test_tool_error_increase_is_hard_fail() -> None:
    baseline = _baseline([_agg(tool_errors_count=0)])
    current = _record([_agg(tool_errors_count=1)])
    hard, _ = compare_to_baseline(baseline, current)
    assert any("tool errors" in h for h in hard)


def test_cli_fail_on_regression_does_not_require_baseline() -> None:
    """--fail-on-regression alone (no --require-baseline, no baseline file committed yet)
    must not hard-fail just because the baseline is missing — that's the whole point of
    decoupling the two flags. It must still fail on this run's own scenario failures."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        no_baseline = tmp_path / "does-not-exist.json"

        passing = _record([_agg(status="passed")])
        run_path = save_run(passing, runs_dir=tmp_path)
        rc = main(
            [
                "--suite",
                "smoke",
                "--run",
                str(run_path),
                "--baseline",
                str(no_baseline),
                "--fail-on-regression",
            ]
        )
        assert rc == 0

        failing = _record([_agg(status="failed", requirement_failures=["nope"])])
        run_path = save_run(failing, runs_dir=tmp_path)
        rc = main(
            [
                "--suite",
                "smoke",
                "--run",
                str(run_path),
                "--baseline",
                str(no_baseline),
                "--fail-on-regression",
            ]
        )
        assert rc == 1


def test_cli_require_baseline_hard_fails_when_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        no_baseline = tmp_path / "does-not-exist.json"
        passing = _record([_agg(status="passed")])
        run_path = save_run(passing, runs_dir=tmp_path)
        rc = main(
            [
                "--suite",
                "smoke",
                "--run",
                str(run_path),
                "--baseline",
                str(no_baseline),
                "--fail-on-regression",
                "--require-baseline",
            ]
        )
        assert rc == 1


def test_suite_aggregate_p95_and_means() -> None:
    scenarios = [
        _agg(scenario_id="a", latency_ms_total=100, quality_scores={"m": 0.5}),
        _agg(scenario_id="b", latency_ms_total=200, quality_scores={"m": 1.0}),
    ]
    agg = build_suite_aggregate(scenarios)
    assert agg.scenario_count == 2
    assert agg.passed_count == 2
    assert agg.mean_quality_by_metric["m"] == 0.75


def test_materialize_workspace_copies_fixture_and_substitutes_subdir() -> None:
    from models import Scenario
    from scenario_loader import materialize_workspace, parse_scenario_yaml

    scenario = parse_scenario_yaml(
        "workspace_fixture: fixtures/coding/calc_repo\n"
        "messages:\n  - 'look in {workspace_subdir}/calc'\n",
        sid="coding/demo",
    )
    assert scenario.workspace_fixture == "fixtures/coding/calc_repo"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved, dest = materialize_workspace(scenario, root)
        assert dest == root / "eval-coding-demo"
        assert (dest / "calc" / "ops.py").is_file()
        assert (dest / "AGENTS.md").is_file()
        assert resolved.messages == ["look in eval-coding-demo/calc"]
        assert scenario.messages == ["look in {workspace_subdir}/calc"]  # input untouched

        (dest / "calc" / "ops.py").write_text("# scribbled\n")
        _, again = materialize_workspace(scenario, root)
        assert "scribbled" not in (again / "calc" / "ops.py").read_text()  # fresh copy per run

    plain, none_dir = materialize_workspace(Scenario(id="p", messages=["hi"]))
    assert none_dir is None and plain.messages == ["hi"]


def test_overlays_are_copied_over_the_fixture_and_define_the_pristine_files() -> None:
    from scenario_loader import fixture_file, materialize_workspace, parse_scenario_yaml

    scenario = parse_scenario_yaml(
        "workspace_fixture: fixtures/coding/shop_repo\n"
        "workspace_overlays: [fixtures/coding/overlays/bug_discount]\n"
        "messages: ['fix {workspace_subdir}']\n",
        sid="coding/ov",
    )
    with tempfile.TemporaryDirectory() as tmp:
        _, dest = materialize_workspace(scenario, Path(tmp))
        pricing = (dest / "shop" / "pricing.py").read_text()
        assert "return int(amount_cents" in pricing  # overlay wins over the green fixture
        assert (dest / "shop" / "taxes.py").is_file()  # untouched fixture files remain
    assert "return int(amount_cents" in fixture_file(scenario, "shop/pricing.py").read_text()
    assert fixture_file(scenario, "shop/taxes.py").is_file()
    assert fixture_file(scenario, "nope.py") is None


def test_workspace_fixture_must_be_relative() -> None:
    from scenario_loader import parse_scenario_yaml

    for bad in ("/etc", "../outside"):
        try:
            parse_scenario_yaml(f"workspace_fixture: {bad}\nmessages: [hi]\n", sid="x")
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_coding_suite_scenarios_all_resolve() -> None:
    from scenario_loader import EVAL_ROOT, fixture_source, load_scenarios_by_id, load_suite

    suite_id, ids = load_suite(EVAL_ROOT / "suites" / "coding.yaml")
    assert suite_id == "coding"
    scenarios = load_scenarios_by_id(ids)
    assert len(scenarios) == 3
    for scenario in scenarios:
        source = fixture_source(scenario)
        assert source is not None and source.is_dir(), scenario.id
        assert "{workspace_subdir}" in scenario.messages[0], scenario.id


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
