"""Requirement + cap assertions evaluated against SSE telemetry, independent of judge score.

Caps (``max_*`` / ``min_score``) and requirements (``required_tools``, ``min_tool_calls``,
``min_subagent_calls``, ``min_summarizations``) are read straight from a scenario's
``assertions`` mapping; keys that aren't present are simply not checked.
"""

from __future__ import annotations

from models import EvalRun, Scenario


def evaluate_assertions(scenario: Scenario, run: EvalRun) -> list[str]:
    """Return human-readable failure strings for caps + requirements; empty when all pass."""
    a = scenario.assertions or {}
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

    return failures
