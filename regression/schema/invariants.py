"""Global invariants (§5.3.2).

Run after every scripted or replay trajectory. A scenario opts out with
``invariants_skip: [{name, reason}]``; a reason is required. Gateway-only
invariants stay quiet on the loop and realtime drivers. The SSE schema check
reads ``tests/contracts/goldens/sse/schema.json`` and does not invent one.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

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
_SCHEMA_ANNOTATIONS = frozenset(
    {
        "description",
        "title",
        "default",
        "examples",
        "$schema",
        "$id",
        "$comment",
        "deprecated",
    }
)
_SCHEMA_KEYWORDS = _SCHEMA_ANNOTATIONS | {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "anyOf",
    "oneOf",
    "allOf",
    "$ref",
    "$defs",
    "definitions",
}


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
    indexes = [
        index for index, event in enumerate(run.events) if event_type(event) == "TurnComplete"
    ]
    if len(indexes) != 1:
        return [f"turn_complete: expected exactly one TurnComplete, saw {len(indexes)}"]
    if indexes[0] != len(run.events) - 1:
        return ["turn_complete: TurnComplete is not the last event"]
    return []


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
    if not schema_path.is_file():
        return [f"sse_schema: schema file not found: {schema_path}"]
    try:
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"sse_schema: schema is not valid JSON ({exc})"]
    if not isinstance(loaded, dict):
        return ["sse_schema: schema root must be an object"]
    failures: list[str] = []
    for index, event in enumerate(_schema_events(run)):
        instance = _as_object(event)
        if instance is None:
            failures.append(f"sse_schema: event {index} is not a JSON object")
            continue
        failures.extend(
            f"sse_schema: event {index} {item}"
            for item in _validate_instance(instance, loaded, loaded, "$")
        )
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


def _validate_instance(
    instance: object,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
) -> list[str]:
    unsupported = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unsupported:
        return [f"{path}: unsupported schema keyword {unsupported}"]
    if "$ref" in schema:
        target = schema["$ref"]
        if not isinstance(target, str):
            return [f"{path}: $ref must be a string"]
        try:
            resolved = _resolve_ref(target, root)
        except KeyError:
            return [f"{path}: unresolvable $ref {target}"]
        return _validate_instance(instance, resolved, root, path)
    errors: list[str] = []
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not in enum {schema['enum']!r}")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: {instance!r} != const {schema['const']!r}")
    if "type" in schema and not _type_matches(instance, schema["type"]):
        errors.append(f"{path}: expected type {schema['type']!r}")
        return errors
    if isinstance(instance, str):
        errors.extend(_string_errors(instance, schema, path))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        errors.extend(_number_errors(instance, schema, path))
    if isinstance(instance, dict):
        errors.extend(_object_errors(instance, schema, root, path))
    if isinstance(instance, list):
        errors.extend(_array_errors(instance, schema, root, path))
    errors.extend(_combiner_errors(instance, schema, root, path))
    return errors


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise KeyError(ref)
    node: object = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise KeyError(ref)
        node = node[part]
    if not isinstance(node, dict):
        raise KeyError(ref)
    return node


def _type_matches(instance: object, expected: object) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    return any(_one_type(instance, name) for name in names if isinstance(name, str))


def _one_type(instance: object, name: str) -> bool:
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "null":
        return instance is None
    return False


def _string_errors(instance: str, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        errors.append(f"{path}: shorter than minLength {minimum}")
    if isinstance(maximum, int) and len(instance) > maximum:
        errors.append(f"{path}: longer than maxLength {maximum}")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.search(pattern, instance) is None:
                errors.append(f"{path}: does not match pattern {pattern!r}")
        except re.error as exc:
            errors.append(f"{path}: invalid pattern {pattern!r} ({exc})")
    return errors


def _number_errors(instance: float, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exclusive_min = schema.get("exclusiveMinimum")
    exclusive_max = schema.get("exclusiveMaximum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and instance < minimum:
        errors.append(f"{path}: {instance} < minimum {minimum}")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and instance > maximum:
        errors.append(f"{path}: {instance} > maximum {maximum}")
    if (
        isinstance(exclusive_min, (int, float))
        and not isinstance(exclusive_min, bool)
        and instance <= exclusive_min
    ):
        errors.append(f"{path}: {instance} <= exclusiveMinimum {exclusive_min}")
    if (
        isinstance(exclusive_max, (int, float))
        and not isinstance(exclusive_max, bool)
        and instance >= exclusive_max
    ):
        errors.append(f"{path}: {instance} >= exclusiveMaximum {exclusive_max}")
    return errors


def _object_errors(
    instance: dict[str, Any],
    schema: Mapping[str, Any],
    root: dict[str, Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in instance:
                errors.append(f"{path}: missing required {name!r}")
    additional = schema.get("additionalProperties", True)
    for name, value in instance.items():
        child = f"{path}.{name}"
        if name in properties and isinstance(properties[name], dict):
            errors.extend(_validate_instance(value, properties[name], root, child))
            continue
        if additional is False:
            errors.append(f"{path}: additional property {name!r}")
        elif isinstance(additional, dict):
            errors.extend(_validate_instance(value, additional, root, child))
    return errors


def _array_errors(
    instance: list[Any],
    schema: Mapping[str, Any],
    root: dict[str, Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        errors.append(f"{path}: fewer than minItems {minimum}")
    if isinstance(maximum, int) and len(instance) > maximum:
        errors.append(f"{path}: more than maxItems {maximum}")
    items = schema.get("items")
    if isinstance(items, dict):
        for index, value in enumerate(instance):
            errors.extend(_validate_instance(value, items, root, f"{path}[{index}]"))
    return errors


def _combiner_errors(
    instance: object,
    schema: Mapping[str, Any],
    root: dict[str, Any],
    path: str,
) -> list[str]:
    errors: list[str] = []
    for name in ("allOf", "anyOf", "oneOf"):
        group = schema.get(name)
        if group is None:
            continue
        if not isinstance(group, list) or not all(isinstance(item, dict) for item in group):
            errors.append(f"{path}: {name} must be a list of schemas")
            continue
        results = [not _validate_instance(instance, item, root, path) for item in group]
        passed = sum(1 for item in results if item)
        ok = (name == "allOf" and passed == len(results)) or (name == "anyOf" and passed > 0)
        if name == "oneOf":
            ok = passed == 1
        if not ok:
            errors.append(f"{path}: did not satisfy {name}")
    return errors


__all__ = [
    "INVARIANT_NAMES",
    "SSE_SCHEMA_PATH",
    "check_invariants",
]
