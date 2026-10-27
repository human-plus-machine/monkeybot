"""Requirement + cap assertions evaluated against SSE telemetry, independent of judge score.

Caps (``max_*`` / ``min_score``) and requirements (``required_tools``, ``min_tool_calls``,
``min_subagent_calls``, ``min_summarizations``, ``min_verdicts``, ``max_verdicts``,
``verdict_status_in``, ``verdict_severity_max``, ``files_not_touched``,
``response_contains``, ``response_regex``, ``response_not_contains``) are read
straight from a scenario's ``assertions`` mapping; keys that aren't present are
simply not checked.

Coding scenarios (a ``workspace_fixture`` copied into the agent workspace) also check the
resulting files: ``workspace_command_passes`` (command exits 0 in the copy),
``workspace_file_contains`` (``{path: substring}``), ``workspace_unchanged`` (paths whose
bytes still equal the fixture), ``final_text_regex`` (last turn's text) and
``verified_before_final`` (the last verification command succeeded and nothing wrote after it).

Nested ``verifier_off`` / ``verifier_on`` blocks are selected by
``EVAL_VERIFIER_MODE`` (``off`` or ``on``). When the env is unset, nested
blocks are ignored and only the top-level keys apply.

Scenario files use the unified spellings (§5.3.1): ``tool_calls`` (``min``, ``max``, and
per-tool counts) and ``verdicts`` (``min``, ``max``, ``status_in``, ``severity_max``,
``signals_include``, ``signals_exclude``) replace the ``*_tool_calls``, ``min_subagent_calls``
and ``*_verdicts`` keys above, which this engine still reads. A key the live runner does not
evaluate is reported as a failure instead of being skipped.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from monkeybot.core.config.settings import VERIFIER_SEVERITY_RANK
from regression.schema.assertions import RunView, evaluate_live_extensions
from regression.t4.models import EvalRun, Scenario, ToolCallRecord, VerdictRecord
from regression.t4.scenario_loader import fixture_file

_NESTED_ASSERTION_KEYS = frozenset({"verifier_off", "verifier_on"})

# Mirrors the default ``coding.verification_gate.verify_commands``.
DEFAULT_VERIFY_REGEX = (
    r"\bpytest\b|\bnpm (run )?test\b|\bpnpm (run )?test\b|\bcargo test\b|\bgo test\b"
    r"|\bmake (test|check)\b|\bruff\b|\bmypy\b|\btsc\b"
)
_WRITE_TOOLS = frozenset({"write_file", "replace_in_file", "apply_patch"})
_WORKSPACE_COMMAND_TIMEOUT_SEC = 180
# ``metrics`` is read by the judge (score.py), not checked here.
_EVALUATED_KEYS = frozenset(
    {
        "metrics",
        "min_score",
        "max_latency_ms",
        "max_input_tokens",
        "max_output_tokens",
        "max_tool_calls",
        "max_tool_errors",
        "required_tools",
        "min_tool_calls",
        "min_subagent_calls",
        "min_summarizations",
        "tool_calls",
        "verdicts",
        "min_verdicts",
        "max_verdicts",
        "verdict_status_in",
        "verdict_severity_max",
        "files_not_touched",
        "response_contains",
        "response_not_contains",
        "response_regex",
        "final_text_regex",
        "verified_before_final",
        "workspace_command_passes",
        "workspace_file_contains",
        "workspace_unchanged",
        "tool_args",
        "per_session",
        "evidence_paths",
        "subagent_manifest",
        "system_prompt",
        "chat_history",
        "sse",
    }
)
_VERDICT_KEYS = frozenset(
    {"min", "max", "status_in", "severity_max", "signals_include", "signals_exclude"}
)


def live_invariant_failures(turn_completes: Sequence[int | None]) -> list[str]:
    """Global live invariants, checked on every live scenario whatever its ``assertions`` say.

    T4-GW-01 ``one_turn_complete_per_request``: each turn's request saw exactly one
    ``TurnComplete``. ``None`` means the run did not count (a run file written before the
    runner did); that turn is not checked.
    """
    return [
        f"one_turn_complete_per_request (T4-GW-01): turn {index} saw {count} TurnComplete events"
        for index, count in enumerate(turn_completes, start=1)
        if count is not None and count != 1
    ]


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]  # type: ignore[union-attr]


def _effective_assertions(raw: dict) -> dict:
    """Merge a ``verifier_on`` / ``verifier_off`` overlay when ``EVAL_VERIFIER_MODE`` is set."""
    mode = os.environ.get("EVAL_VERIFIER_MODE", "").strip().lower()
    nested_key = {"on": "verifier_on", "off": "verifier_off"}.get(mode)
    overlay = raw.get(nested_key) if nested_key else None
    if not isinstance(overlay, dict):
        return {k: v for k, v in raw.items() if k not in _NESTED_ASSERTION_KEYS}
    merged = {k: v for k, v in raw.items() if k not in _NESTED_ASSERTION_KEYS}
    merged.update(overlay)
    return merged


def _path_args_from_run(run: EvalRun) -> list[str]:
    paths: list[str] = []
    for turn in run.turns:
        for call in turn.tool_calls:
            paths.extend(call.path_args)
    return paths


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _run_workspace_command(command: str, cwd: Path) -> tuple[bool, str]:
    """Run ``command`` (argv-split, no shell) in ``cwd``; ``python`` means this interpreter."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return False, f"unparseable command: {exc}"
    if not argv:
        return False, "empty command"
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_COMMAND_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    tail = (proc.stdout + proc.stderr).strip()[-400:]
    return proc.returncode == 0, f"exit {proc.returncode}: {tail}"


def _command_line(call: ToolCallRecord) -> str:
    """``run_command`` args as one shell-ish line (``["npm","test"]`` -> ``npm test``)."""
    try:
        args = json.loads(call.args_summary)
    except ValueError:
        return call.args_summary
    if not isinstance(args, dict):
        return call.args_summary
    argv = args.get("argv")
    if isinstance(argv, list) and argv:
        return " ".join(str(part) for part in argv)
    for key in ("command", "cmd", "script", "shell"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            extra = args.get("args")
            tail = " ".join(str(a) for a in extra) if isinstance(extra, list) else ""
            return f"{value} {tail}".strip()
    return call.args_summary


def _verified_before_final(calls: list[ToolCallRecord], verify_regex: str) -> str | None:
    """Failure text unless the last verify command passed and no write came after it."""
    try:
        pattern = re.compile(verify_regex)
    except re.error as exc:
        return f"invalid verify regex {verify_regex!r} ({exc})"
    last_verify = None
    for idx, call in enumerate(calls):
        if call.tool == "run_command" and pattern.search(_command_line(call)):
            last_verify = idx
    if last_verify is None:
        return "no verification command was run"
    if calls[last_verify].ok is not True:
        return "the last verification command did not pass"
    wrote_after = [c.tool for c in calls[last_verify + 1 :] if c.tool in _WRITE_TOOLS]
    if wrote_after:
        return f"files were modified after the last passing verification ({wrote_after})"
    return None


def _run_view(run: EvalRun) -> RunView:
    """The recorded run as the shared engine sees it. Sparse fields stay empty."""
    session_events: dict[int, list[object]] = {}
    for turn in run.turns:
        if turn.session_index < 1:
            continue
        session_events.setdefault(turn.session_index, []).extend(turn.events)
    workspace = run.agent_workspace or run.workspace_dir
    return RunView(
        events=[event for turn in run.turns for event in turn.events],
        workspace=workspace,
        sse_frames=[frame for turn in run.turns for frame in turn.frames],
        scores=dict(run.scores),
        chat_history=list(run.history_snapshots),
        system_prompts=list(run.system_prompts),
        manifests=list(run.manifests),
        turn_inputs=[turn.input for turn in run.turns],
        session_events=session_events,
    )


def evaluate_assertions(scenario: Scenario, run: EvalRun) -> list[str]:
    """Return human-readable failure strings for caps + requirements; empty when all pass."""
    a = _effective_assertions(scenario.assertions or {})
    failures: list[str] = []

    unevaluated = sorted(set(a) - _EVALUATED_KEYS)
    if unevaluated:
        failures.append(f"assertion keys not evaluated by the live runner: {unevaluated}")

    usage = run.usage_total()
    tool_names = set(run.tool_calls_by_name())
    tool_calls_count = run.tool_calls_count()
    tool_errors_count = run.tool_errors_count()

    if "min_score" in a and run.scores:
        threshold = float(a["min_score"])
        worst = min(run.scores.values())
        if worst < threshold:
            failures.append(f"min_score: worst judge score {worst:.3f} < {threshold}")

    if "max_latency_ms" in a and usage.duration_ms > int(a["max_latency_ms"]):
        failures.append(f"max_latency_ms: {usage.duration_ms} > {a['max_latency_ms']}")

    if "max_input_tokens" in a and usage.input_tokens > int(a["max_input_tokens"]):
        failures.append(f"max_input_tokens: {usage.input_tokens} > {a['max_input_tokens']}")

    if "max_output_tokens" in a and usage.output_tokens > int(a["max_output_tokens"]):
        failures.append(f"max_output_tokens: {usage.output_tokens} > {a['max_output_tokens']}")

    if "max_tool_calls" in a and tool_calls_count > int(a["max_tool_calls"]):
        failures.append(f"max_tool_calls: {tool_calls_count} > {a['max_tool_calls']}")

    if "max_tool_errors" in a and tool_errors_count > int(a["max_tool_errors"]):
        failures.append(f"max_tool_errors: {tool_errors_count} > {a['max_tool_errors']}")

    if "required_tools" in a:
        required = {str(t) for t in a["required_tools"]}
        missing = required - tool_names
        if missing:
            failures.append(
                f"required_tools: missing {sorted(missing)} (called: {sorted(tool_names)})"
            )

    if "min_tool_calls" in a and tool_calls_count < int(a["min_tool_calls"]):
        failures.append(f"min_tool_calls: {tool_calls_count} < {a['min_tool_calls']}")

    if "min_subagent_calls" in a and run.subagent_calls_count() < int(a["min_subagent_calls"]):
        failures.append(
            f"min_subagent_calls: {run.subagent_calls_count()} < {a['min_subagent_calls']}"
        )

    if "min_summarizations" in a and run.summarizations_count() < int(a["min_summarizations"]):
        failures.append(
            f"min_summarizations: {run.summarizations_count()} < {a['min_summarizations']}"
        )

    if "tool_calls" in a:
        failures.extend(_tool_calls_failures(a["tool_calls"], run))

    verdicts = run.verdicts()
    verdicts_count = run.verdicts_count()

    if "verdicts" in a:
        failures.extend(_verdicts_failures(a["verdicts"], verdicts))

    if "min_verdicts" in a and verdicts_count < int(a["min_verdicts"]):
        failures.append(f"min_verdicts: {verdicts_count} < {a['min_verdicts']}")

    if "max_verdicts" in a and verdicts_count > int(a["max_verdicts"]):
        failures.append(f"max_verdicts: {verdicts_count} > {a['max_verdicts']}")

    if "verdict_status_in" in a:
        allowed = {str(s) for s in a["verdict_status_in"]}
        unexpected = sorted({v.status for v in verdicts if v.status not in allowed})
        if unexpected:
            failures.append(
                f"verdict_status_in: unexpected statuses {unexpected} (allowed: {sorted(allowed)})"
            )

    if "verdict_severity_max" in a:
        cap = str(a["verdict_severity_max"])
        cap_rank = VERIFIER_SEVERITY_RANK.get(cap)
        if cap_rank is None:
            failures.append(f"verdict_severity_max: unknown severity {cap!r}")
        else:
            over = [
                v.severity
                for v in verdicts
                if VERIFIER_SEVERITY_RANK.get(v.severity, cap_rank + 1) > cap_rank
            ]
            if over:
                failures.append(f"verdict_severity_max: {over} exceeded cap {cap!r}")

    if "files_not_touched" in a:
        globs = _as_list(a["files_not_touched"])
        touched = _path_args_from_run(run)
        hits = sorted({p for p in touched if any(fnmatch.fnmatch(p, g) for g in globs)})
        if hits:
            failures.append(f"files_not_touched: matched {hits} against {globs}")

    combined = " ".join(t.output for t in run.turns)
    combined_lower = combined.lower()

    if "response_contains" in a:
        phrases = [p.lower() for p in _as_list(a["response_contains"])]
        if phrases and not any(p in combined_lower for p in phrases):
            failures.append(f"response_contains: none of {phrases} found in the agent's output")

    if "response_not_contains" in a:
        hits = [p for p in _as_list(a["response_not_contains"]) if p.lower() in combined_lower]
        if hits:
            failures.append(
                f"response_not_contains: forbidden phrases {hits} found in the agent's output"
            )

    if "response_regex" in a:
        patterns = _as_list(a["response_regex"])
        bad_patterns: list[str] = []
        matched = False
        for p in patterns:
            try:
                # Every pattern must be checked regardless of `matched` — an `or`
                # short-circuit here would skip re.search (and its re.error) once a
                # prior pattern already matched.
                if re.search(p, combined, re.IGNORECASE):
                    matched = True
            except re.error as exc:
                bad_patterns.append(f"{p!r} ({exc})")
        if bad_patterns:
            failures.append(f"response_regex: invalid pattern(s) {bad_patterns}")
        elif patterns and not matched:
            failures.append(f"response_regex: none of {patterns} matched the agent's output")

    if "final_text_regex" in a:
        final_text = run.turns[-1].output if run.turns else ""
        patterns = _as_list(a["final_text_regex"])
        try:
            if not any(re.search(p, final_text) for p in patterns):
                failures.append(f"final_text_regex: none of {patterns} matched the final answer")
        except re.error as exc:
            failures.append(f"final_text_regex: invalid pattern ({exc})")

    if "verified_before_final" in a and a["verified_before_final"]:
        flag = a["verified_before_final"]
        verify_regex = flag if isinstance(flag, str) else DEFAULT_VERIFY_REGEX
        calls = [c for t in run.turns for c in t.tool_calls]
        problem = _verified_before_final(calls, verify_regex)
        if problem:
            failures.append(f"verified_before_final: {problem}")

    workspace_keys = ("workspace_command_passes", "workspace_file_contains", "workspace_unchanged")
    if any(key in a for key in workspace_keys):
        failures.extend(_workspace_failures(scenario, run, a))

    failures.extend(evaluate_live_extensions(a, _run_view(run)))
    return failures


def _bounds(value: object) -> tuple[int | None, int | None] | None:
    """``(min, max)`` from an exact int or ``{min, max}``; None when the shape is wrong."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value, value
    if not isinstance(value, dict) or set(value) - {"min", "max"}:
        return None
    low, high = value.get("min"), value.get("max")
    for bound in (low, high):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
            return None
    return low, high


def _count_failure(label: str, count: int, low: int | None, high: int | None) -> str | None:
    if low is not None and count < low:
        return f"{label}: {count} < {low}"
    if high is not None and count > high:
        return f"{label}: {count} > {high}"
    return None


def _tool_calls_failures(spec: object, run: EvalRun) -> list[str]:
    """``tool_calls``: ``min`` / ``max`` bound the total; any other key bounds one tool's count."""
    if not isinstance(spec, dict):
        return ["tool_calls: expected {min, max, <tool>: count}"]
    total = _bounds({k: v for k, v in spec.items() if k in {"min", "max"}})
    if total is None:
        return ["tool_calls: min and max must be integers"]
    failures: list[str] = []
    message = _count_failure("tool_calls", run.tool_calls_count(), *total)
    if message:
        failures.append(message)
    counts = run.tool_calls_by_name()
    for name, expected in spec.items():
        if name in {"min", "max"}:
            continue
        parsed = _bounds(expected)
        if parsed is None:
            failures.append(f"tool_calls: {name} count must be an integer or {{min, max}}")
            continue
        message = _count_failure(f"tool_calls.{name}", counts.get(str(name), 0), *parsed)
        if message:
            failures.append(message)
    return failures


def _verdicts_failures(spec: object, verdicts: list[VerdictRecord]) -> list[str]:
    if not isinstance(spec, dict):
        return ["verdicts: expected a mapping"]
    unknown = sorted(set(spec) - _VERDICT_KEYS)
    if unknown:
        return [f"verdicts: unknown matcher {unknown}"]
    failures: list[str] = []
    count = _bounds({k: v for k, v in spec.items() if k in {"min", "max"}})
    if count is None:
        failures.append("verdicts: min and max must be integers")
    else:
        message = _count_failure("verdicts", len(verdicts), *count)
        if message:
            failures.append(message)
    if "status_in" in spec:
        allowed = {str(s) for s in _as_list(spec["status_in"])}
        unexpected = sorted({v.status for v in verdicts if v.status not in allowed})
        if unexpected:
            failures.append(f"verdicts: unexpected statuses {unexpected}")
    if "severity_max" in spec:
        cap = str(spec["severity_max"])
        cap_rank = VERIFIER_SEVERITY_RANK.get(cap)
        if cap_rank is None:
            failures.append(f"verdicts: unknown severity cap {cap!r}")
        else:
            over = [
                v.severity
                for v in verdicts
                if VERIFIER_SEVERITY_RANK.get(v.severity, cap_rank + 1) > cap_rank
            ]
            if over:
                failures.append(f"verdicts: {over} exceeded cap {cap!r}")
    seen = {signal for v in verdicts for signal in v.triggering_signals}
    if "signals_include" in spec:
        missing = [s for s in _as_list(spec["signals_include"]) if s not in seen]
        if missing:
            failures.append(f"verdicts: signals_include missing {missing}")
    if "signals_exclude" in spec:
        hits = sorted(seen & set(_as_list(spec["signals_exclude"])))
        if hits:
            failures.append(f"verdicts: signals_exclude saw {hits}")
    return failures


def _workspace_failures(scenario: Scenario, run: EvalRun, a: dict) -> list[str]:
    workspace = run.workspace_dir
    if workspace is None or not workspace.is_dir():
        return ["workspace_*: no workspace directory was provided for this run"]
    failures: list[str] = []

    for command in _as_list(a.get("workspace_command_passes", [])):
        passed, detail = _run_workspace_command(command, workspace)
        if not passed:
            failures.append(f"workspace_command_passes: `{command}` failed ({detail})")

    for rel, needle in (a.get("workspace_file_contains") or {}).items():
        try:
            text = (workspace / rel).read_text(encoding="utf-8")
        except OSError:
            failures.append(f"workspace_file_contains: {rel} is missing or unreadable")
            continue
        if str(needle) not in text:
            failures.append(f"workspace_file_contains: {rel} does not contain {needle!r}")

    if "workspace_unchanged" in a:
        for rel in _as_list(a["workspace_unchanged"]):
            pristine = fixture_file(scenario, rel)
            original = _file_digest(pristine) if pristine is not None else None
            current = _file_digest(workspace / rel)
            if original is None:
                failures.append(f"workspace_unchanged: {rel} is not in the fixture")
            elif current != original:
                failures.append(f"workspace_unchanged: {rel} was modified or deleted")
    return failures
