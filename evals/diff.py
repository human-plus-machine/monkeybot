"""Log-diff two eval runs: pinpoint the scenario and prompt/response pair behind a slip.

    uv run python -m evals.diff                     # latest two runs in evals/runs/
    uv run python -m evals.diff old.json new.json   # explicit run files

Compares per-scenario status, judge scores, and requirement failures, then prints a
unified diff of the agent's output for every turn of each regressed scenario. Exits 1
when any scenario regressed (passed before, not passed now, newly errored, or dropped
out of the new run entirely).
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from persistence import RUNS_DIR, RunRecord, ScenarioAggregate, load_run  # noqa: E402

# Judge scores below this delta are treated as noise, matching compare_to_baseline.
SCORE_NOISE = 0.05


def _latest_two(runs_dir: Path = RUNS_DIR) -> tuple[Path, Path]:
    candidates = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if len(candidates) < 2:
        raise SystemExit(f"Need two run files in {runs_dir} (found {len(candidates)}); pass paths explicitly.")
    return candidates[-2], candidates[-1]


def _regressed(old: ScenarioAggregate, new: ScenarioAggregate) -> bool:
    return (old.status == "passed" and new.status != "passed") or (
        old.status != "errored" and new.status == "errored"
    )


def _turn_diff_lines(old: ScenarioAggregate, new: ScenarioAggregate) -> list[str]:
    lines: list[str] = []
    for i, nt in enumerate(new.turns):
        ot = old.turns[i] if i < len(old.turns) else None
        old_out = ot.output if ot else ""
        if ot and ot.output == nt.output:
            continue
        lines.append(f"  turn {i}: {nt.input!r}")
        diff = difflib.unified_diff(
            old_out.splitlines(), nt.output.splitlines(),
            fromfile="old", tofile="new", lineterm="",
        )
        lines.extend(f"    {d}" for d in diff)
    if not lines:
        lines.append("  (no turn text recorded — re-run with a monkeybot version that logs turns)")
    return lines


def diff_runs(old: RunRecord, new: RunRecord) -> tuple[str, bool]:
    """Return ``(report_text, any_regression)``."""
    old_by_id = {s.scenario_id: s for s in old.scenarios}
    new_by_id = {s.scenario_id: s for s in new.scenarios}
    lines = [
        f"old: {old.run_id}  ({old.git_sha[:9] or 'no-sha'} {old.started_at})",
        f"new: {new.run_id}  ({new.git_sha[:9] or 'no-sha'} {new.started_at})",
        "",
    ]
    any_regression = False

    for sid in sorted(set(old_by_id) | set(new_by_id)):
        o, n = old_by_id.get(sid), new_by_id.get(sid)
        if o is None or n is None:
            lines.append(f"{sid}: only in {'new' if o is None else 'old'} run")
            if o is not None:
                # A scenario present before and gone now is itself a regression worth
                # blocking on, not just a note (e.g. dropped from the suite manifest).
                any_regression = True
            continue

        notes: list[str] = []
        if o.status != n.status:
            notes.append(f"status {o.status} -> {n.status}")
        for metric in sorted(set(o.quality_scores) | set(n.quality_scores)):
            ov, nv = o.quality_scores.get(metric), n.quality_scores.get(metric)
            if ov is not None and nv is not None and abs(nv - ov) > SCORE_NOISE:
                notes.append(f"{metric} {ov:.3f} -> {nv:.3f}")
        new_failures = [f for f in n.requirement_failures if f not in o.requirement_failures]
        notes.extend(f"new failure: {f}" for f in new_failures)
        if n.failure_reason and n.failure_reason != o.failure_reason:
            notes.append(f"error: {n.failure_reason}")

        if not notes:
            continue
        lines.append(f"{sid}:")
        lines.extend(f"  {note}" for note in notes)
        if _regressed(o, n):
            any_regression = True
            lines.extend(_turn_diff_lines(o, n))
        lines.append("")

    if len(lines) == 3:
        lines.append("No differences beyond noise.")
    return "\n".join(lines), any_regression


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="*", type=Path, help="Two run JSON files (default: latest two)")
    args = parser.parse_args(argv)

    if len(args.runs) == 2:
        old_path, new_path = args.runs
    elif not args.runs:
        old_path, new_path = _latest_two()
    else:
        parser.error("pass zero or two run files")

    report, regression = diff_runs(load_run(old_path), load_run(new_path))
    print(report)
    return 1 if regression else 0


if __name__ == "__main__":
    raise SystemExit(main())
