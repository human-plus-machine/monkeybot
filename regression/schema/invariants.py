"""Global invariants (§5.3.2).

Run after every scripted or replay trajectory. A scenario opts out with
``invariants_skip: [{name, reason}]``; a reason is required. Gateway-only
invariants stay quiet on the loop and realtime drivers. The SSE schema check
reads ``tests/contracts/goldens/sse/schema.json`` and does not invent one.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from monkeybot.core.runtime.events import EventDecodeError, event_to_json
from regression.schema.assertions import RunView, event_field, event_type, history_orphan_ids

SSE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "contracts" / "goldens" / "sse" / "schema.json"
)

INVARIANT_NAMES: frozenset[str] = frozenset(
    {
        "turn_complete",
        "tool_call_results",
        "no_tool_after_cancel",
        "history_pairs",
        "no_sse_intervention",
        "sse_schema",
        "strict_script",
        "turn_lock",
    }
)

_CANCEL_TYPES = frozenset({"Cancel", "Cancelled", "RunCancelled"})


def check_invariants(
    run: RunView,
    skips: Sequence[object] | None = None,
    *,
    schema_path: Path | None = None,
) -> list[str]:
    """Return one string per failed invariant. Empty means every applicable one holds.

    Raises ``ValueError`` when a skip has no reason or names an unknown invariant.
    """
    skipped = _skip_names(skips)
    failures: list[str] = []
    if "turn_complete" not in skipped:
        failures.extend(_turn_complete(run))
    if "tool_call_results" not in skipped:
        failures.extend(_tool_call_results(run))
    if "no_tool_after_cancel" not in skipped:
        failures.extend(_no_tool_after_cancel(run))
    if "history_pairs" not in skipped:
        failures.extend(_history_pairs(run))
    gateway = run.driver == "gateway"
    if gateway and "no_sse_intervention" not in skipped:
        failures.extend(_no_sse_intervention(run))
    if gateway and "sse_schema" not in skipped:
        failures.extend(_sse_schema(run, SSE_SCHEMA_PATH if schema_path is None else schema_path))
    if run.scripted and "strict_script" not in skipped:
        failures.extend(_strict_script(run))
    if gateway and "turn_lock" not in skipped:
        failures.extend(_turn_lock(run))
    return failures


def _skip_names(skips: Sequence[object] | None) -> frozenset[str]:
    names: list[str] = []
    for item in skips or []:
        if isinstance(item, Mapping):
            name = item.get("name")
            reason = item.get("reason")
        else:
            name = getattr(item, "name", None)
            reason = getattr(item, "reason", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invariants_skip name is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("invariants_skip reason is required")
        cleaned = name.strip()
        if cleaned not in INVARIANT_NAMES:
            raise ValueError(f"unknown invariant {cleaned!r}")
        names.append(cleaned)
    return frozenset(names)


def _turn_complete(run: RunView) -> list[str]:
    """One TurnComplete per request, and the trajectory ends on one.

    Two user messages in one session are two requests. One request that
    completes twice is still a failure.
    """
    completes = [event for event in run.events if event_type(event) == "TurnComplete"]
    if not completes:
        return ["turn_complete: expected exactly one TurnComplete, saw 0"]
    if event_type(run.events[-1]) != "TurnComplete":
        return ["turn_complete: TurnComplete is not the last event"]
    counts: Counter[str] = Counter()
    for event in completes:
        request_id = event_field(event, "request_id")
        counts[request_id if isinstance(request_id, str) else ""] += 1
    failures: list[str] = []
    for request_id, count in sorted(counts.items()):
        if count != 1:
            label = request_id or "(none)"
            failures.append(f"turn_complete: request {label} has {count} TurnComplete events")
    return failures


def _tool_call_results(run: RunView) -> list[str]:
    starts: Counter[str] = Counter()
    results: Counter[str] = Counter()
    for event in run.events:
        kind = event_type(event)
        call_id = event_field(event, "call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if kind == "ToolCallStarted":
            starts[call_id] += 1
        elif kind == "ToolCallResult":
            results[call_id] += 1
    failures: list[str] = []
    for call_id, count in sorted(starts.items()):
        result_count = results[call_id]
        if count != 1 or result_count != 1:
            failures.append(
                f"tool_call_results: call_id {call_id!r} has {count} ToolCallStarted"
                f" and {result_count} ToolCallResult"
            )
    return failures


def _is_cancel(event: object) -> bool:
    kind = event_type(event)
    if kind in _CANCEL_TYPES:
        return True
    if kind != "Error":
        return False
    text = event_field(event, "error")
    if not isinstance(text, str):
        return False
    lowered = text.strip().lower()
    return lowered == "request cancelled" or lowered.startswith("request cancelled")


def _no_tool_after_cancel(run: RunView) -> list[str]:
    cancel_at: int | None = None
    for index, event in enumerate(run.events):
        if _is_cancel(event):
            cancel_at = index
            break
    if cancel_at is None:
        return []
    late = [
        index
        for index, event in enumerate(run.events)
        if index > cancel_at and event_type(event) == "ToolCallStarted"
    ]
    if late:
        return [f"no_tool_after_cancel: ToolCallStarted after cancel at indexes {late}"]
    return []


def _history_pairs(run: RunView) -> list[str]:
    missing_responses, missing_requests = history_orphan_ids(run.history)
    if not missing_responses and not missing_requests:
        return []
    return [
        "history_pairs: orphan tool ids "
        f"(requests without responses {missing_responses}, "
        f"responses without requests {missing_requests})"
    ]


def _wire_events(run: RunView) -> list[object]:
    if run.sse_frames:
        return list(run.sse_frames)
    if run.serialized_events is not None:
        return list(run.serialized_events)
    return list(run.events)


def _no_sse_intervention(run: RunView) -> list[str]:
    hits = [
        index
        for index, event in enumerate(_wire_events(run))
        if event_type(event) == "HarnessIntervention"
    ]
    if hits:
        return [f"no_sse_intervention: HarnessIntervention on the SSE wire at {hits}"]
    return []


def _strict_script(run: RunView) -> list[str]:
    if run.script_partial or run.script_remaining == 0:
        return []
    return [
        f"strict_script: {run.script_remaining} scripted call(s) were not consumed"
        " (set script_partial: true to allow a leftover script)"
    ]


def _turn_lock(run: RunView) -> list[str]:
    status = run.second_reply_status
    if status is None:
        return ["turn_lock: second /reply was not issued"]
    if status == 409:
        return ["turn_lock: second /reply returned 409"]
    return []


def _sse_schema(run: RunView, schema_path: Path) -> list[str]:
    """Invariant 6. Every serialized gateway event validates against the golden schema."""
    if not schema_path.is_file():
        return [f"sse_schema: schema file not found: {schema_path}"]
    try:
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"sse_schema: schema is not valid JSON ({exc})"]
    if not isinstance(loaded, dict):
        return ["sse_schema: schema root must be an object"]
    try:
        jsonschema.Draft202012Validator.check_schema(loaded)
    except jsonschema.SchemaError as exc:
        return [f"sse_schema: schema is not valid ({exc.message})"]
    validator = jsonschema.Draft202012Validator(loaded)
    failures: list[str] = []
    for index, event in enumerate(_schema_events(run)):
        instance = _as_object(event)
        if instance is None:
            failures.append(f"sse_schema: event {index} is not a JSON object")
            continue
        messages = [error.message for error in validator.iter_errors(instance)]
        if messages:
            failures.append(f"sse_schema: event {index} {'; '.join(messages)}")
    return failures


def _schema_events(run: RunView) -> list[object]:
    if run.serialized_events is not None:
        return list(run.serialized_events)
    serialized: list[object] = []
    source = run.sse_frames if run.sse_frames else run.events
    for event in source:
        if isinstance(event, dict):
            serialized.append(event)
            continue
        try:
            raw = event_to_json(event)
        except (TypeError, ValueError, EventDecodeError) as exc:
            serialized.append({"__unserializable__": str(exc)})
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            serialized.append({"__unserializable__": raw})
            continue
        serialized.append(parsed if isinstance(parsed, dict) else {"__value__": parsed})
    return serialized


def _as_object(event: object) -> dict[str, Any] | None:
    if isinstance(event, dict):
        return event
    if isinstance(event, str):
        try:
            parsed = json.loads(event)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


__all__ = [
    "INVARIANT_NAMES",
    "SSE_SCHEMA_PATH",
    "check_invariants",
]
