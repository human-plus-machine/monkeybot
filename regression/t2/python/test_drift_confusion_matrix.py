"""T2-VER-23: precision and recall of the scripted drift corpus.

VER-01 through VER-10 are the corpus. A signal's positives are the stems that
must emit it. Every other stem is a negative. ``no_progress`` and ``budget_burn``
have no positives: they score 1.0 only when every drift scenario ran and none
of them put the signal on a verdict. A failed positive is a false negative. A
failed negative is not a false positive. The thresholds stay at 1.0.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from regression.drivers.loop import run_scenario
from regression.schema.assertions import event_field, event_type
from regression.schema.scenario import load_scenario

_DIR = Path(__file__).resolve().parents[1] / "loop" / "scenarios" / "verifier"

_SIGNALS = (
    "write_without_read",
    "error_streak",
    "rewrite_churn",
    "constraint_touch",
    "repeat_correction",
    "done_unmet",
    "no_progress",
    "budget_burn",
)

_DRIFT_STEMS = (
    "write_without_read_positive",
    "write_without_read_negative",
    "error_streak_positive",
    "error_streak_negative",
    "rewrite_churn_positive",
    "constraint_touch_positive",
    "constraint_touch_negative",
    "free_text_constraint_never_matches",
    "done_unmet_positive",
    "repeat_correction_positive",
    "preempt_negative",
    "supersession_negative",
    "no_progress_budget_burn_not_mailboxed",
    "min_turn_before_verdict",
)

_POSITIVES: dict[str, frozenset[str]] = {
    "write_without_read": frozenset({"write_without_read_positive", "min_turn_before_verdict"}),
    "error_streak": frozenset({"error_streak_positive"}),
    "rewrite_churn": frozenset({"rewrite_churn_positive"}),
    "constraint_touch": frozenset({"constraint_touch_positive", "min_turn_before_verdict"}),
    "repeat_correction": frozenset({"repeat_correction_positive"}),
    "done_unmet": frozenset({"done_unmet_positive"}),
    "no_progress": frozenset(),
    "budget_burn": frozenset(),
}


@dataclass(frozen=True)
class _Outcome:
    stem: str
    case: str
    signals: frozenset[str] | None
    error: str | None


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    detail = text[0] if text else type(exc).__name__
    return f"{type(exc).__name__}: {detail}"


def _signals(events: list[object]) -> frozenset[str]:
    found: set[str] = set()
    for event in events:
        if event_type(event) != "VerifierVerdict":
            continue
        raw = event_field(event, "triggering_signals")
        if isinstance(raw, (list, tuple)):
            found.update(item for item in raw if isinstance(item, str))
    return frozenset(found)


async def _run_all() -> list[_Outcome]:
    outcomes: list[_Outcome] = []
    for stem in _DRIFT_STEMS:
        path = _DIR / f"{stem}.yaml"
        scenario = load_scenario(path)
        try:
            result = await run_scenario(scenario)
        except Exception as exc:
            outcomes.append(_Outcome(stem, scenario.case, None, _one_line(exc)))
            continue
        outcomes.append(_Outcome(stem, scenario.case, _signals(result.events), None))
    return outcomes


def _score(signal: str, outcomes: list[_Outcome]) -> tuple[float, float, str]:
    positives = _POSITIVES[signal]
    if not positives:
        failed = [item for item in outcomes if item.error is not None]
        emitted = [item for item in outcomes if item.signals is not None and signal in item.signals]
        if failed or emitted:
            notes = [f"{item.stem} ({item.case}): {item.error}" for item in failed]
            notes.extend(f"{item.stem} ({item.case}): unexpected {signal}" for item in emitted)
            return 0.0, 0.0, "; ".join(notes)
        return 1.0, 1.0, ""

    true_positive = 0
    false_positive = 0
    false_negative = 0
    notes: list[str] = []
    for item in outcomes:
        expected = item.stem in positives
        if item.error is not None:
            if expected:
                false_negative += 1
                notes.append(f"{item.stem} ({item.case}): {item.error}")
            continue
        seen = signal in (item.signals or frozenset())
        if expected and seen:
            true_positive += 1
        elif expected:
            false_negative += 1
            notes.append(f"{item.stem} ({item.case}): missing {signal}")
        elif seen:
            false_positive += 1
            notes.append(f"{item.stem} ({item.case}): unexpected {signal}")
    precision_denom = true_positive + false_positive
    recall_denom = true_positive + false_negative
    precision = true_positive / precision_denom if precision_denom else 0.0
    recall = true_positive / recall_denom if recall_denom else 0.0
    return precision, recall, "; ".join(notes)


@pytest.mark.case("T2-VER-23")
def test_drift_confusion_matrix() -> None:
    outcomes = asyncio.run(_run_all())
    misses: list[str] = []
    for signal in _SIGNALS:
        precision, recall, detail = _score(signal, outcomes)
        if precision == 1.0 and recall == 1.0:
            continue
        misses.append(f"{signal}: precision {precision:.1f} recall {recall:.1f}; {detail}")
    assert not misses, "drift confusion matrix is not 1.0:\n" + "\n".join(misses)
