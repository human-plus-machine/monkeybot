"""Assert-based self-check for evals/scorer.py's pure judge_expected logic (no network).

Run: python evals/test_scorer.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import Scenario  # noqa: E402
from scorer import judge_expected  # noqa: E402


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


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok: {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
