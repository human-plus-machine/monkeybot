"""Requirement + cap assertions evaluated against SSE telemetry, independent of judge score.

Caps (``max_*`` / ``min_score``) and requirements (``required_tools``, ``min_tool_calls``,
``min_subagent_calls``, ``min_summarizations``, ``min_verdicts``, ``max_verdicts``,
``verdict_status_in``, ``verdict_severity_max``, ``files_not_touched``,
``response_contains``, ``response_regex``, ``response_not_contains``) are read
straight from a scenario's ``assertions`` mapping; keys that aren't present are
simply not checked.

Nested ``verifier_off`` / ``verifier_on`` blocks are selected by
``EVAL_VERIFIER_MODE`` (``off`` or ``on``). When the env is unset, nested
blocks are ignored and only the top-level keys apply.
"""

from __future__ import annotations

import fnmatch
import os
import re

from models import EvalRun, Scenario

from monkeybot.core.config.settings import VERIFIER_SEVERITY_RANK

_NESTED_ASSERTION_KEYS = frozenset({"verifier_off", "verifier_on"})


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


def evaluate_assertions(scenario: Scenario, run: EvalRun) -> list[str]:
    """Return human-readable failure strings for caps + requirements; empty when all pass."""
    a = _effective_assertions(scenario.assertions or {})
    failures: list[str] = []

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

    verdicts = run.verdicts()
    verdicts_count = run.verdicts_count()

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

    return failures
