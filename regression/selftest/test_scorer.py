"""Assert-based self-check for regression/t4/score.py's pure judge_expected logic (no network)."""

from __future__ import annotations

import os

from regression.t4.models import Scenario
from regression.t4.score import judge_expected


def _scenario(assertions: dict) -> Scenario:
    return Scenario(id="s", messages=["hi"], assertions=assertions)


def test_default_metrics_expect_a_judge() -> None:
    os.environ.pop("JUDGE_PROVIDER", None)
    assert judge_expected(_scenario({})) is True


def test_explicitly_empty_metrics_do_not_expect_a_judge() -> None:
    os.environ.pop("JUDGE_PROVIDER", None)
    assert judge_expected(_scenario({"metrics": []})) is False


def test_disabled_judge_provider_does_not_expect_a_judge() -> None:
    os.environ["JUDGE_PROVIDER"] = "fake"
    try:
        assert judge_expected(_scenario({})) is False
    finally:
        del os.environ["JUDGE_PROVIDER"]


def test_enabled_judge_provider_expects_a_judge() -> None:
    os.environ["JUDGE_PROVIDER"] = "nvidia"
    try:
        assert judge_expected(_scenario({"metrics": ["turn_relevancy"]})) is True
    finally:
        del os.environ["JUDGE_PROVIDER"]
