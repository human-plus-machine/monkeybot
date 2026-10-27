"""Assertion engine (§5.3.1).

One implementation for every mode. :func:`validate_assertions` rejects a key
used in the wrong mode, and ``verifier_on`` / ``verifier_off`` while
``EVAL_VERIFIER_MODE`` is unset. :func:`evaluate` checks a finished
:class:`RunView`. It does not import the live runner.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from monkeybot.core.config.settings import VERIFIER_SEVERITY_RANK
from monkeybot.core.hooks.evidence_guard import extract_evidence_paths
from monkeybot.core.llm.provider import Message
from monkeybot.core.messages.tool_integrity import (
    cancelled_tool_result_text,
    interrupted_tool_result_text,
)
from monkeybot.core.persistence.thread_summary import is_context_summary_row
from monkeybot.core.tools.inspector import norm_run_command_line
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse

# Mirrors the default ``coding.verification_gate.verify_commands``.
DEFAULT_VERIFY_REGEX = (
    r"\bpytest\b|\bnpm (run )?test\b|\bpnpm (run )?test\b|\bcargo test\b|\bgo test\b"
    r"|\bmake (test|check)\b|\bruff\b|\bmypy\b|\btsc\b"
)
WORKSPACE_COMMAND_TIMEOUT_SEC = 180

_VERIFIER_KEYS = frozenset({"verifier_on", "verifier_off"})
_VERIFIER_MODES = frozenset({"on", "off"})
_COMMAND_TOOLS = frozenset({"run_command", "await_command", "kill_command"})
_WRITE_TOOLS = frozenset({"write_file", "replace_in_file", "apply_patch"})
_PATH_ARG_KEYS = ("resolved_path", "path", "file_path", "file", "root", "dir")
_READ_TOOLS = frozenset({"read_file", "list_files", "find_files", "list_dir"})
_WRITE_CATEGORY = frozenset({"write_file", "create_file", "replace_in_file", "apply_patch"})
_LOOP_TOOLS = frozenset({"start_loop", "loop_status", "pause_loop", "resume_loop", "stop_loop"})
_CODE_SEARCH_TOOLS = frozenset({"glob", "grep"})

# None means every mode. ``sse`` is special: live, or a gateway driver.
_MODES_ALL: frozenset[str] | None = None
_ASSERTION_MODES: dict[str, frozenset[str] | None] = {
    "turn_completed": _MODES_ALL,
    "no_errors": _MODES_ALL,
    "error_contains": _MODES_ALL,
    "events_sequence": frozenset({"scripted", "replay"}),
    "context_usage_drops": frozenset({"scripted", "replay"}),
    "events_absent": _MODES_ALL,
    "event_fields": _MODES_ALL,
    "interventions": frozenset({"scripted", "replay"}),
    "interventions_include": frozenset({"scripted", "replay"}),
    "provider_calls": frozenset({"scripted", "replay"}),
    "provider_request": frozenset({"scripted", "replay"}),
    "required_tools": _MODES_ALL,
    "forbidden_tools": _MODES_ALL,
    "tool_categories_used": _MODES_ALL,
    "tool_calls": _MODES_ALL,
    "tool_error_codes": _MODES_ALL,
    "tool_error_codes_absent": _MODES_ALL,
    "max_tool_errors": _MODES_ALL,
    "workspace_file_contains": _MODES_ALL,
    "workspace_file_absent": _MODES_ALL,
    "workspace_unchanged": _MODES_ALL,
    "workspace_command_passes": _MODES_ALL,
    "files_not_touched": _MODES_ALL,
    "verified_before_final": _MODES_ALL,
    "history": frozenset({"scripted", "replay"}),
    "store": frozenset({"scripted", "replay"}),
    "memory_injected": _MODES_ALL,
    "verdicts": _MODES_ALL,
    "subagent": _MODES_ALL,
    "sse": frozenset(),
    "response_contains": _MODES_ALL,
    "response_contains_all": _MODES_ALL,
    "response_not_contains": _MODES_ALL,
    "response_regex": _MODES_ALL,
    "final_text_regex": _MODES_ALL,
    "metrics": frozenset({"live"}),
    "min_score": frozenset({"live"}),
    "min_summarizations": frozenset({"live"}),
    "max_latency_ms": frozenset({"live"}),
    "max_input_tokens": frozenset({"live"}),
    "max_output_tokens": frozenset({"live"}),
    "max_cost_usd": frozenset({"live"}),
    "verifier_on": frozenset({"live"}),
    "verifier_off": frozenset({"live"}),
    "tool_args": frozenset({"live"}),
    "per_session": frozenset({"live"}),
    "evidence_paths": frozenset({"live"}),
    "subagent_manifest": frozenset({"live"}),
    "system_prompt": frozenset({"live"}),
    "chat_history": frozenset({"live"}),
}
# Keys a ``per_session`` block cannot hold: the judge scores the whole run, not one session.
_PER_SESSION_BANNED = frozenset(
    {"per_session", "verifier_on", "verifier_off", "metrics", "min_score"}
)

_HISTORY_KEYS = frozenset(
    {
        "row_count",
        "roles",
        "tool_pairs_intact",
        "summary_row",
        "row_ids_preserved",
        "no_repaired_rows",
        "blocks_include",
    }
)
_STORE_KEYS = frozenset({"outbox", "usage", "loops", "goals", "branches"})
_SSE_KEYS = frozenset(
    {
        "frames_have_ids",
        "active_requests_once",
        "one_turn_complete_per_request",
        "replay_from",
        "golden",
        "deltas_equal_final",
    }
)
_VERDICT_KEYS = frozenset(
    {"min", "max", "status_in", "severity_max", "signals_include", "signals_exclude"}
)
_SUBAGENT_KEYS = frozenset(
    {
        "started",
        "completed_ok",
        "artifact_exists",
        "progress_jsonl_lines_min",
        "output_json_present",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "n",
        "tools_include",
        "tools_exclude",
        "tools_empty",
        "system_contains",
        "system_not_contains",
        "messages_contain",
        "messages_not_contain",
        "no_vertex_google_search",
        "thinking_budget",
        "model",
    }
)
_FRAME_ID_SKIP = frozenset({"ActiveRequests", "ping"})


@dataclass
class ProviderRequest:
    """One ``stream()`` call the assertion matchers can see."""

    tools: list[str] = field(default_factory=list)
    system: str = ""
    messages: list[str] = field(default_factory=list)
    vertex_google_search: bool = False
    thinking_budget: int | None = None
    model: str = ""


@dataclass
class StoreView:
    """Backend row counts the ``store`` assertion reads."""

    outbox: int = 0
    usage: int = 0
    loops: int = 0
    goals: int = 0
    branches: int = 0


@dataclass
class RunView:
    """What one scenario run produced. Drivers fill this; the engine only reads it."""

    events: list[Any] = field(default_factory=list)
    workspace: Path | None = None
    fixture_dir: Path | None = None
    memory_text: str = ""
    provider_calls: int | None = None
    provider_requests: list[ProviderRequest] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    store: StoreView | None = None
    scores: dict[str, float] = field(default_factory=dict)
    subagent_dir: Path | None = None
    sse_frames: list[Any] = field(default_factory=list)
    sse_replay: dict[str, list[str]] = field(default_factory=dict)
    serialized_events: list[Any] | None = None
    scripted: bool = False
    script_remaining: int = 0
    script_partial: bool = False
    driver: str | None = None
    second_reply_status: int | None = None
    sse_wire: str = ""
    # Filled by the live runner. Empty means that check was not recorded.
    chat_history: list[list[dict[str, Any]]] = field(default_factory=list)
    system_prompts: list[str] = field(default_factory=list)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    turn_inputs: list[str] = field(default_factory=list)
    session_events: dict[int, list[Any]] = field(default_factory=dict)


@dataclass
class ObservedCall:
    tool: str
    args: dict[str, Any]
    resolved_path: str | None
    call_id: str
    ok: bool | None = None
    error: str | None = None
    error_code: str | None = None


def tool_category(tool_name: str) -> str:
    """Map a core tool name to the coarse category scenarios name."""
    if tool_name in _READ_TOOLS:
        return "read"
    if tool_name in _WRITE_CATEGORY:
        return "write"
    if tool_name == "run_command":
        return "shell"
    if tool_name == "task":
        return "subagent"
    if tool_name in _LOOP_TOOLS:
        return "loop"
    if tool_name == "web_search":
        return "web_search"
    if tool_name == "todo_list":
        return "todo"
    if tool_name == "load_file":
        return "attachment"
    if tool_name in _CODE_SEARCH_TOOLS:
        return "code_search"
    return "other"


def event_type(event: object) -> str:
    """Discriminator, whether the event is a dataclass (``kind``) or a dict (``type``)."""
    if isinstance(event, dict):
        raw = event.get("type", event.get("kind", ""))
        return raw if isinstance(raw, str) else ""
    kind = getattr(event, "kind", None)
    return kind if isinstance(kind, str) else ""


def event_field(event: object, name: str) -> Any:
    """One field. Dict keys win over attributes so wire payloads stay literal."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def canonical_target(tool: str, args: Mapping[str, Any], resolved_path: str | None = None) -> str:
    """Path or command that identifies one tool target.

    An error is forgiven only by a later success of the same tool on this same
    target. The tool name alone is not enough.
    """
    if tool in _COMMAND_TOOLS:
        line = _command_line(args)
        return f"cmd:{line}" if line else "cmd:"
    path = ""
    if isinstance(resolved_path, str) and resolved_path.strip():
        path = resolved_path
    else:
        path = _path_from_args(args) or ""
    if path:
        return "path:" + _norm_path(path)
    return ""


def history_orphan_ids(messages: Sequence[Message]) -> tuple[list[str], list[str]]:
    """Tool request ids with no response, then response ids with no request."""
    requests: set[str] = set()
    responses: set[str] = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolRequest) and block.id:
                requests.add(block.id)
            elif isinstance(block, ToolResponse) and block.id:
                responses.add(block.id)
    missing_responses = sorted(requests - responses)
    missing_requests = sorted(responses - requests)
    return missing_responses, missing_requests


def validate_assertions(
    assertions: dict[str, Any] | list[Any] | None,
    *,
    mode: str,
    driver: str | None,
) -> None:
    """Reject assertion keys that do not belong to ``mode`` (and a gateway-only ``sse``).

    A list is the unparsed form the scenario model already accepts; it has no
    keys to check. ``verifier_on`` / ``verifier_off`` on a live scenario are a
    load error while ``EVAL_VERIFIER_MODE`` is unset.
    """
    if not isinstance(assertions, dict):
        return
    problems: list[str] = []
    verifier_present = False
    for key, value in assertions.items():
        problems.extend(_key_problems(key, mode=mode, driver=driver, where=""))
        if key == "per_session" and _key_allowed(key, mode=mode, driver=driver):
            problems.extend(_per_session_problems(value, mode=mode, driver=driver))
            continue
        if key not in _VERIFIER_KEYS:
            if (
                key == "memory_injected"
                and not problems_for_key(problems, key)
                and not _memory_shape(value)
            ):
                problems.append("memory_injected must be a mapping with contains")
            continue
        if not _key_allowed(key, mode=mode, driver=driver):
            continue
        verifier_present = True
        if not isinstance(value, dict):
            problems.append(f"{key} must be a mapping")
            continue
        for inner_key, inner_value in value.items():
            if inner_key in _VERIFIER_KEYS:
                problems.append(f"{inner_key} cannot be nested")
                continue
            problems.extend(_key_problems(inner_key, mode=mode, driver=driver, where=key))
            if (
                inner_key == "memory_injected"
                and _key_allowed(inner_key, mode=mode, driver=driver)
                and not _memory_shape(inner_value)
            ):
                problems.append(f"{key}.memory_injected must be a mapping with contains")
    if verifier_present and _verifier_mode() is None:
        problems.append(
            "EVAL_VERIFIER_MODE is unset; verifier_on and verifier_off are a load error"
        )
    if problems:
        raise ValueError("; ".join(problems))


def evaluate(assertions: dict[str, Any] | list[Any] | None, run: RunView) -> list[str]:
    """Return human-readable failures. Empty means every present key holds."""
    if not isinstance(assertions, dict):
        return []
    active = _effective_assertions(assertions)
    failures: list[str] = []
    checks = (
        _check_turn_completed,
        _check_no_errors,
        _check_error_contains,
        _check_events_sequence,
        _check_context_usage_drops,
        _check_events_absent,
        _check_event_fields,
        _check_interventions,
        _check_interventions_include,
        _check_provider_calls,
        _check_provider_request,
        _check_required_tools,
        _check_forbidden_tools,
        _check_tool_categories,
        _check_tool_calls,
        _check_tool_error_codes,
        _check_tool_error_codes_absent,
        _check_max_tool_errors,
        _check_workspace,
        _check_files_not_touched,
        _check_verified_before_final,
        _check_history,
        _check_store,
        _check_memory_injected,
        _check_verdicts,
        _check_subagent,
        _check_sse,
        _check_response_contains,
        _check_response_contains_all,
        _check_response_not_contains,
        _check_response_regex,
        _check_final_text_regex,
        _check_metrics,
        _check_min_score,
        _check_min_summarizations,
        _check_tool_args,
        _check_per_session,
        _check_evidence_paths,
        _check_subagent_manifest,
        _check_system_prompt,
        _check_chat_history,
        _check_budgets,
    )
    for check in checks:
        failures.extend(check(active, run))
    return failures


def problems_for_key(problems: list[str], key: str) -> bool:
    """True when ``problems`` already explains ``key`` (used to avoid a second shape error)."""
    needle = f"'{key}'"
    return any(needle in item or item.startswith(key) for item in problems)


def _verifier_mode() -> str | None:
    mode = os.environ.get("EVAL_VERIFIER_MODE", "").strip().lower()
    if mode in _VERIFIER_MODES:
        return mode
    return None


def _effective_assertions(raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay the verifier block selected by ``EVAL_VERIFIER_MODE``."""
    selected = {"on": "verifier_on", "off": "verifier_off"}.get(_verifier_mode() or "")
    merged = {key: value for key, value in raw.items() if key not in _VERIFIER_KEYS}
    overlay = raw.get(selected) if selected else None
    if isinstance(overlay, dict):
        merged.update(overlay)
    return merged


def _key_allowed(key: str, *, mode: str, driver: str | None) -> bool:
    if key == "sse":
        return mode == "live" or driver == "gateway"
    allowed = _ASSERTION_MODES.get(key)
    if key not in _ASSERTION_MODES:
        return False
    if allowed is None:
        return True
    return mode in allowed


def _key_problems(key: str, *, mode: str, driver: str | None, where: str) -> list[str]:
    label = f"{where}.{key}" if where else key
    if key not in _ASSERTION_MODES:
        return [f"unknown assertion key {label!r}"]
    if _key_allowed(key, mode=mode, driver=driver):
        return []
    if key == "sse":
        shown = driver if driver is not None else "unset"
        return [f"assertion key {label!r} is not legal for mode {mode!r} with driver {shown!r}"]
    return [f"assertion key {label!r} is not legal for mode {mode!r}"]


def _per_session_problems(value: object, *, mode: str, driver: str | None) -> list[str]:
    """``per_session`` maps a 1-based session number to that session's assertion keys."""
    if not isinstance(value, dict) or not value:
        return ["per_session must map session numbers to assertion mappings"]
    problems: list[str] = []
    for number, block in value.items():
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            problems.append(f"per_session key {number!r} must be a session number from 1")
            continue
        where = f"per_session.{number}"
        if not isinstance(block, dict) or not block:
            problems.append(f"{where} must be a non-empty mapping")
            continue
        for inner_key in block:
            if inner_key in _PER_SESSION_BANNED:
                problems.append(f"{inner_key} cannot be nested in {where}")
                continue
            problems.extend(_key_problems(inner_key, mode=mode, driver=driver, where=where))
    return problems


def _memory_shape(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"contains"}:
        return False
    needle = value.get("contains")
    if isinstance(needle, str):
        return True
    return isinstance(needle, list) and all(isinstance(item, str) for item in needle)


def _as_str_list(value: object) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _events(run: RunView) -> list[Any]:
    return run.events


def _usage_totals(run: RunView) -> dict[str, float]:
    totals = {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cost_usd": 0.0,
        "duration_ms": 0.0,
    }
    for event in _events(run):
        if event_type(event) != "TurnComplete":
            continue
        usage = event_field(event, "usage")
        if usage is None:
            continue
        for key in totals:
            raw = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, 0)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                totals[key] += float(raw)
    return totals


def _response_text(run: RunView) -> str:
    ended = [
        text
        for event in _events(run)
        if event_type(event) == "AssistantTextEnded"
        and isinstance(text := event_field(event, "text"), str)
        and text
    ]
    if ended:
        return "\n".join(ended)
    deltas: list[str] = []
    for event in _events(run):
        if event_type(event) != "AssistantDelta":
            continue
        delta = event_field(event, "delta")
        if isinstance(delta, str):
            deltas.append(delta)
    return "".join(deltas)


def _final_text(run: RunView) -> str:
    ended = [
        text
        for event in _events(run)
        if event_type(event) == "AssistantTextEnded"
        and isinstance(text := event_field(event, "text"), str)
    ]
    if ended:
        return ended[-1]
    return _response_text(run)


def _error_texts(run: RunView) -> list[str]:
    texts: list[str] = []
    for event in _events(run):
        if event_type(event) != "Error":
            continue
        text = event_field(event, "error")
        if isinstance(text, str):
            texts.append(text)
    return texts


def _interventions(run: RunView) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for event in _events(run):
        if event_type(event) != "HarnessIntervention":
            continue
        name = event_field(event, "intervention")
        detail = event_field(event, "detail")
        found.append(
            (
                name if isinstance(name, str) else "",
                detail if isinstance(detail, str) else "",
            )
        )
    return found


def _observed_calls(run: RunView) -> list[ObservedCall]:
    by_id: dict[str, ObservedCall] = {}
    order: list[ObservedCall] = []
    anon = 0
    for event in _events(run):
        kind = event_type(event)
        if kind == "ToolCallStarted":
            raw_id = event_field(event, "call_id")
            call_id = raw_id if isinstance(raw_id, str) and raw_id else ""
            if not call_id:
                anon += 1
                call_id = f"anon-{anon}"
            args = event_field(event, "args")
            tool = event_field(event, "tool")
            resolved = event_field(event, "resolved_path")
            call = ObservedCall(
                tool=tool if isinstance(tool, str) else "",
                args=dict(args) if isinstance(args, dict) else {},
                resolved_path=resolved if isinstance(resolved, str) else None,
                call_id=call_id,
            )
            by_id[call_id] = call
            order.append(call)
            continue
        if kind != "ToolCallResult":
            continue
        raw_id = event_field(event, "call_id")
        call_id = raw_id if isinstance(raw_id, str) else ""
        matched = by_id.get(call_id)
        if matched is None:
            continue
        error = event_field(event, "error")
        result = event_field(event, "result")
        failed, code = _result_failed(
            result if isinstance(result, str) else "",
            error if isinstance(error, str) else None,
        )
        matched.ok = not failed
        matched.error = error if isinstance(error, str) and error else None
        matched.error_code = code
    return order


def _json_object(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object. Anything else is no envelope."""
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _details_code(payload: dict[str, Any] | None) -> str | None:
    """``details.code`` when it is a string. A missing or non-string code is none."""
    if payload is None:
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    raw_code = details.get("code")
    if isinstance(raw_code, str):
        return raw_code
    return None


def _result_failed(result: str, error: str | None) -> tuple[bool, str | None]:
    """Whether the call failed, and ``details.code`` when an envelope has one.

    The result body wins. The loop puts a failed tool's JSON envelope on
    ``error`` and leaves ``result`` empty, which is what the old runner parsed.
    A non-JSON error string is still a failure and contributes no code.
    """
    failed = bool(error)
    body = _json_object(result)
    if body is not None and body.get("ok") is False:
        failed = True
    code = _details_code(body)
    if code is None:
        code = _details_code(_json_object(error or ""))
    return failed, code


def _command_line(args: Mapping[str, Any]) -> str:
    try:
        line = norm_run_command_line(dict(args))
    except ValueError:
        return ""
    return line or ""


def _path_from_args(args: Mapping[str, Any]) -> str | None:
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _norm_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts:
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _path_args(call: ObservedCall) -> list[str]:
    found: list[str] = []
    if call.resolved_path:
        found.append(call.resolved_path)
    for key in _PATH_ARG_KEYS:
        value = call.args.get(key)
        if isinstance(value, str) and value not in found:
            found.append(value)
    return found


def _field_matches(actual: object, expected: object) -> bool:
    if isinstance(expected, dict) and set(expected) == {"regex"}:
        pattern = expected.get("regex")
        if not isinstance(pattern, str) or not isinstance(actual, str):
            return False
        try:
            return re.search(pattern, actual) is not None
        except re.error:
            return False
    if isinstance(expected, dict):
        if actual is None or isinstance(actual, (str, bytes, int, float, bool)):
            return False
        for key, want in expected.items():
            if not isinstance(key, str):
                return False
            got = actual.get(key) if isinstance(actual, dict) else getattr(actual, key, None)
            if not _field_matches(got, want):
                return False
        return True
    return actual == expected


def _match_fields(event: object, fields: Mapping[str, Any]) -> bool:
    for name, expected in fields.items():
        if name == "type":
            continue
        if not _field_matches(event_field(event, name), expected):
            return False
    return True


def _expected_interventions(value: object) -> list[tuple[str, str | None]] | str:
    if not isinstance(value, list):
        return "expected a list"
    parsed: list[tuple[str, str | None]] = []
    for item in value:
        if isinstance(item, str):
            parsed.append((item, None))
            continue
        if isinstance(item, dict):
            name = item.get("intervention", item.get("name"))
            if not isinstance(name, str):
                return "each item needs an intervention name"
            detail = item.get("detail")
            parsed.append((name, detail if isinstance(detail, str) else None))
            continue
        return "each item must be a name or {intervention, detail}"
    return parsed


def _unresolved_errors(calls: Sequence[ObservedCall]) -> int:
    unresolved = 0
    for index, call in enumerate(calls):
        if call.ok is not False:
            continue
        target = canonical_target(call.tool, call.args, call.resolved_path)
        forgiven = False
        for later in calls[index + 1 :]:
            if later.tool != call.tool or later.ok is not True:
                continue
            later_target = canonical_target(later.tool, later.args, later.resolved_path)
            if later_target == target:
                forgiven = True
                break
        if not forgiven:
            unresolved += 1
    return unresolved


def _error_codes(calls: Sequence[ObservedCall]) -> set[str]:
    return {call.error_code for call in calls if call.error_code}


def _verdicts(run: RunView) -> list[tuple[str, str, tuple[str, ...]]]:
    found: list[tuple[str, str, tuple[str, ...]]] = []
    for event in _events(run):
        if event_type(event) != "VerifierVerdict":
            continue
        status = event_field(event, "status")
        severity = event_field(event, "severity")
        signals = event_field(event, "triggering_signals")
        signal_list: list[str] = []
        if isinstance(signals, (list, tuple)):
            signal_list = [item for item in signals if isinstance(item, str)]
        found.append(
            (
                status if isinstance(status, str) else "",
                severity if isinstance(severity, str) else "",
                tuple(signal_list),
            )
        )
    return found


def _bounds(value: object) -> tuple[int | None, int | None, bool] | None:
    """Return ``(min, max, exact_from_int)``. None when the shape is wrong."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value, value, True
    if isinstance(value, dict):
        low = value.get("min")
        high = value.get("max")
        if low is not None and (isinstance(low, bool) or not isinstance(low, int)):
            return None
        if high is not None and (isinstance(high, bool) or not isinstance(high, int)):
            return None
        if set(value) - {"min", "max"}:
            return None
        return (
            low if isinstance(low, int) else None,
            high if isinstance(high, int) else None,
            False,
        )
    return None


def _count_failure(label: str, count: int, low: int | None, high: int | None) -> str | None:
    if low is not None and count < low:
        return f"{label}: {count} < {low}"
    if high is not None and count > high:
        return f"{label}: {count} > {high}"
    return None


def _check_turn_completed(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "turn_completed" not in active:
        return []
    flag = active["turn_completed"]
    if flag is not True:
        return []
    completed = any(event_type(event) == "TurnComplete" for event in _events(run))
    if completed:
        return []
    return ["turn_completed: no TurnComplete event"]


def _check_no_errors(active: Mapping[str, Any], run: RunView) -> list[str]:
    if active.get("no_errors") is not True:
        return []
    errors = _error_texts(run)
    if not errors:
        return []
    return [f"no_errors: saw {errors}"]


def _check_error_contains(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "error_contains" not in active:
        return []
    needles = _as_str_list(active["error_contains"])
    if needles is None:
        return ["error_contains: expected a string or list of strings"]
    errors = _error_texts(run)
    missing = [needle for needle in needles if not any(needle in text for text in errors)]
    if missing:
        return [f"error_contains: {missing} not found in {errors}"]
    return []


def _check_events_sequence(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "events_sequence" not in active:
        return []
    spec = active["events_sequence"]
    if not isinstance(spec, list):
        return ["events_sequence: expected a list"]
    cursor = 0
    events = _events(run)
    for step in spec:
        if isinstance(step, str):
            kind, fields = step, {}
        elif isinstance(step, dict) and isinstance(step.get("type"), str):
            kind = str(step["type"])
            fields = {key: value for key, value in step.items() if key != "type"}
        else:
            return ["events_sequence: each step must be a type or {type, ...}"]
        found = False
        while cursor < len(events):
            event = events[cursor]
            cursor += 1
            if event_type(event) == kind and _match_fields(event, fields):
                found = True
                break
        if not found:
            return [f"events_sequence: {kind} was not found in order"]
    return []


def _check_context_usage_drops(active: Mapping[str, Any], run: RunView) -> list[str]:
    """True when a later ContextUsage is smaller than one before ContextSummarizing."""
    if active.get("context_usage_drops") is not True:
        return []
    usages: list[tuple[int, int]] = []
    summarizing: list[int] = []
    for index, event in enumerate(_events(run)):
        kind = event_type(event)
        if kind == "ContextSummarizing":
            summarizing.append(index)
            continue
        if kind != "ContextUsage":
            continue
        tokens = event_field(event, "estimated_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            usages.append((index, tokens))
    for position, (start, before) in enumerate(usages):
        for end, after in usages[position + 1 :]:
            if after < before and any(start < marker < end for marker in summarizing):
                return []
    return ["context_usage_drops: estimated_tokens did not fall after ContextSummarizing"]


def _check_events_absent(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "events_absent" not in active:
        return []
    banned = _as_str_list(active["events_absent"])
    if banned is None:
        return ["events_absent: expected a string or list of strings"]
    present = sorted({event_type(event) for event in _events(run)} & set(banned))
    if present:
        return [f"events_absent: saw {present}"]
    return []


def _check_event_fields(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "event_fields" not in active:
        return []
    spec = active["event_fields"]
    if not isinstance(spec, dict):
        return ["event_fields: expected {type: {field: value}}"]
    failures: list[str] = []
    for kind, fields in spec.items():
        if not isinstance(kind, str) or not isinstance(fields, dict):
            failures.append(f"event_fields: {kind!r} must map to field matchers")
            continue
        matched = any(
            event_type(event) == kind and _match_fields(event, fields) for event in _events(run)
        )
        if not matched:
            failures.append(f"event_fields: no {kind} matched {fields}")
    return failures


def _check_interventions(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "interventions" not in active:
        return []
    expected = _expected_interventions(active["interventions"])
    if isinstance(expected, str):
        return [f"interventions: {expected}"]
    actual = _interventions(run)
    if len(actual) != len(expected):
        return [
            "interventions: expected "
            f"{[name for name, _detail in expected]}, saw {[name for name, _detail in actual]}"
        ]
    for index, ((want_name, want_detail), (got_name, got_detail)) in enumerate(
        zip(expected, actual, strict=True)
    ):
        if got_name != want_name or (want_detail is not None and got_detail != want_detail):
            return [
                f"interventions: item {index} expected {want_name!r}"
                f"{'' if want_detail is None else ' detail ' + want_detail!r},"
                f" saw {got_name!r} detail {got_detail!r}"
            ]
    return []


def _check_interventions_include(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "interventions_include" not in active:
        return []
    expected = _expected_interventions(active["interventions_include"])
    if isinstance(expected, str):
        return [f"interventions_include: {expected}"]
    actual = _interventions(run)
    missing: list[str] = []
    for name, detail in expected:
        found = any(
            got_name == name and (detail is None or got_detail == detail)
            for got_name, got_detail in actual
        )
        if not found:
            missing.append(name if detail is None else f"{name} ({detail})")
    if missing:
        return [f"interventions_include: missing {missing} (saw {[name for name, _d in actual]})"]
    return []


def _check_provider_calls(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "provider_calls" not in active:
        return []
    expected = active["provider_calls"]
    if isinstance(expected, bool) or not isinstance(expected, int):
        return ["provider_calls: expected an integer"]
    actual = run.provider_calls
    if actual is None:
        actual = len(run.provider_requests)
    if actual != expected:
        return [f"provider_calls: {actual} != {expected}"]
    return []


def _check_provider_request(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "provider_request" not in active:
        return []
    spec = active["provider_request"]
    if not isinstance(spec, list):
        return ["provider_request: expected a list of matchers"]
    failures: list[str] = []
    for index, matcher in enumerate(spec):
        if not isinstance(matcher, dict):
            failures.append(f"provider_request[{index}]: expected a mapping")
            continue
        unknown = sorted(set(matcher) - _REQUEST_KEYS)
        if unknown:
            failures.append(f"provider_request[{index}]: unknown matcher {unknown}")
            continue
        number = matcher.get("n", index + 1)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            failures.append(f"provider_request[{index}]: n must be a 1-based index")
            continue
        if number > len(run.provider_requests):
            failures.append(f"provider_request: no request {number}")
            continue
        failures.extend(_match_provider_request(number, matcher, run.provider_requests[number - 1]))
    return failures


def _match_provider_request(
    number: int, matcher: Mapping[str, Any], request: ProviderRequest
) -> list[str]:
    failures: list[str] = []
    label = f"provider_request[{number}]"
    tools = set(request.tools)
    include = matcher.get("tools_include")
    if include is not None:
        wanted = _as_str_list(include)
        if wanted is None:
            failures.append(f"{label}: tools_include must be a list of names")
        else:
            missing = [name for name in wanted if name not in tools]
            if missing:
                failures.append(f"{label}: tools_include missing {missing}")
    exclude = matcher.get("tools_exclude")
    if exclude is not None:
        banned = _as_str_list(exclude)
        if banned is None:
            failures.append(f"{label}: tools_exclude must be a list of names")
        else:
            hits = [name for name in banned if name in tools]
            if hits:
                failures.append(f"{label}: tools_exclude saw {hits}")
    if "tools_empty" in matcher:
        flag = matcher["tools_empty"]
        if not isinstance(flag, bool):
            failures.append(f"{label}: tools_empty must be a boolean")
        elif flag and request.tools:
            failures.append(f"{label}: tools_empty but saw {request.tools}")
        elif not flag and not request.tools:
            failures.append(f"{label}: tools_empty is false but the tool list is empty")
    system = request.system
    contains = matcher.get("system_contains")
    if isinstance(contains, str) and contains not in system:
        failures.append(f"{label}: system_contains {contains!r} not in the system prompt")
    elif contains is not None and not isinstance(contains, str):
        failures.append(f"{label}: system_contains must be a string")
    absent = matcher.get("system_not_contains")
    if isinstance(absent, str) and absent in system:
        failures.append(f"{label}: system_not_contains {absent!r} was in the system prompt")
    elif absent is not None and not isinstance(absent, str):
        failures.append(f"{label}: system_not_contains must be a string")
    messages_needle = matcher.get("messages_contain")
    blob = "\n".join(request.messages)
    if isinstance(messages_needle, str) and messages_needle not in blob:
        failures.append(f"{label}: messages_contain {messages_needle!r} not in the messages")
    elif messages_needle is not None and not isinstance(messages_needle, str):
        failures.append(f"{label}: messages_contain must be a string")
    messages_absent = matcher.get("messages_not_contain")
    if isinstance(messages_absent, str) and messages_absent in blob:
        failures.append(f"{label}: messages_not_contain {messages_absent!r} was in the messages")
    elif messages_absent is not None and not isinstance(messages_absent, str):
        failures.append(f"{label}: messages_not_contain must be a string")
    expected_model = matcher.get("model")
    if isinstance(expected_model, str) and request.model != expected_model:
        failures.append(f"{label}: model {request.model!r} != {expected_model!r}")
    elif expected_model is not None and not isinstance(expected_model, str):
        failures.append(f"{label}: model must be a string")
    if matcher.get("no_vertex_google_search") is True and request.vertex_google_search:
        failures.append(f"{label}: vertex google search was enabled")
    if "thinking_budget" in matcher:
        expected_budget = matcher["thinking_budget"]
        if expected_budget is not None and (
            isinstance(expected_budget, bool) or not isinstance(expected_budget, int)
        ):
            failures.append(f"{label}: thinking_budget must be an integer or null")
        elif request.thinking_budget != expected_budget:
            failures.append(
                f"{label}: thinking_budget {request.thinking_budget} != {expected_budget}"
            )
    return failures


def _check_required_tools(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "required_tools" not in active:
        return []
    required = _as_str_list(active["required_tools"])
    if required is None:
        return ["required_tools: expected a list of tool names"]
    called = {call.tool for call in _observed_calls(run)}
    missing = sorted(set(required) - called)
    if missing:
        return [f"required_tools: missing {missing} (called: {sorted(called)})"]
    return []


def _check_forbidden_tools(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "forbidden_tools" not in active:
        return []
    banned = _as_str_list(active["forbidden_tools"])
    if banned is None:
        return ["forbidden_tools: expected a list of tool names"]
    called = {call.tool for call in _observed_calls(run)}
    hits = sorted(called & set(banned))
    if hits:
        return [f"forbidden_tools: called {hits}"]
    return []


def _check_tool_categories(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "tool_categories_used" not in active:
        return []
    listed = _as_str_list(active["tool_categories_used"])
    if listed is None:
        return ["tool_categories_used: expected a list of categories"]
    used = {tool_category(call.tool) for call in _observed_calls(run)}
    missing = sorted(set(listed) - used)
    if missing:
        return [f"tool_categories_used: missing {missing} (used: {sorted(used)})"]
    return []


def _check_tool_calls(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "tool_calls" not in active:
        return []
    spec = active["tool_calls"]
    if not isinstance(spec, dict):
        return ["tool_calls: expected {min, max, <tool>: count}"]
    calls = _observed_calls(run)
    failures: list[str] = []
    low = spec.get("min")
    high = spec.get("max")
    if low is not None and (isinstance(low, bool) or not isinstance(low, int)):
        return ["tool_calls: min must be an integer"]
    if high is not None and (isinstance(high, bool) or not isinstance(high, int)):
        return ["tool_calls: max must be an integer"]
    total = _count_failure(
        "tool_calls",
        len(calls),
        low if isinstance(low, int) else None,
        high if isinstance(high, int) else None,
    )
    if total:
        failures.append(total)
    counts: dict[str, int] = {}
    for call in calls:
        counts[call.tool] = counts.get(call.tool, 0) + 1
    for name, expected in spec.items():
        if name in {"min", "max"}:
            continue
        parsed = _bounds(expected)
        if parsed is None:
            failures.append(f"tool_calls: {name} count must be an integer or {{min, max}}")
            continue
        bound_low, bound_high, _exact = parsed
        message = _count_failure(f"tool_calls.{name}", counts.get(name, 0), bound_low, bound_high)
        if message:
            failures.append(message)
    return failures


def _check_tool_error_codes(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "tool_error_codes" not in active:
        return []
    wanted = _as_str_list(active["tool_error_codes"])
    if wanted is None:
        return ["tool_error_codes: expected a list of codes"]
    seen = _error_codes(_observed_calls(run))
    missing = [code for code in wanted if code not in seen]
    if missing:
        return [f"tool_error_codes: missing {missing} (saw {sorted(seen)})"]
    return []


def _check_tool_error_codes_absent(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "tool_error_codes_absent" not in active:
        return []
    banned = _as_str_list(active["tool_error_codes_absent"])
    if banned is None:
        return ["tool_error_codes_absent: expected a list of codes"]
    seen = _error_codes(_observed_calls(run))
    hits = sorted(seen & set(banned))
    if hits:
        return [f"tool_error_codes_absent: saw {hits}"]
    return []


def _check_max_tool_errors(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "max_tool_errors" not in active:
        return []
    limit = active["max_tool_errors"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        return ["max_tool_errors: expected an integer"]
    count = _unresolved_errors(_observed_calls(run))
    if count > limit:
        return [f"max_tool_errors: {count} unresolved > {limit}"]
    return []


def _check_workspace(active: Mapping[str, Any], run: RunView) -> list[str]:
    keys = (
        "workspace_file_contains",
        "workspace_file_absent",
        "workspace_unchanged",
        "workspace_command_passes",
    )
    if not any(key in active for key in keys):
        return []
    workspace = run.workspace
    if workspace is None or not workspace.is_dir():
        return ["workspace_*: no workspace directory was provided for this run"]
    failures: list[str] = []
    contains = active.get("workspace_file_contains") or {}
    if "workspace_file_contains" in active and not isinstance(contains, dict):
        failures.append("workspace_file_contains: expected {path: substring}")
        contains = {}
    for rel, needle in contains.items():
        if not isinstance(rel, str):
            failures.append("workspace_file_contains: paths must be strings")
            continue
        try:
            text = (workspace / rel).read_text(encoding="utf-8")
        except OSError:
            failures.append(f"workspace_file_contains: {rel} is missing or unreadable")
            continue
        if str(needle) not in text:
            failures.append(f"workspace_file_contains: {rel} does not contain {needle!r}")
    if "workspace_file_absent" in active:
        absent = _as_str_list(active["workspace_file_absent"])
        if absent is None:
            failures.append("workspace_file_absent: expected a path or list of paths")
        else:
            for rel in absent:
                if (workspace / rel).exists():
                    failures.append(f"workspace_file_absent: {rel} exists")
    if "workspace_unchanged" in active:
        unchanged = _as_str_list(active["workspace_unchanged"])
        if unchanged is None:
            failures.append("workspace_unchanged: expected a path or list of paths")
        else:
            for rel in unchanged:
                failures.extend(_unchanged_failure(run, workspace, rel))
    if "workspace_command_passes" in active:
        commands = _as_str_list(active["workspace_command_passes"])
        if commands is None:
            failures.append("workspace_command_passes: expected a command or list of commands")
        else:
            for command in commands:
                passed, detail = _run_workspace_command(command, workspace)
                if not passed:
                    failures.append(f"workspace_command_passes: `{command}` failed ({detail})")
    return failures


def _unchanged_failure(run: RunView, workspace: Path, rel: str) -> list[str]:
    if run.fixture_dir is None:
        return [f"workspace_unchanged: no fixture directory for {rel}"]
    original = _file_digest(run.fixture_dir / rel)
    current = _file_digest(workspace / rel)
    if original is None:
        return [f"workspace_unchanged: {rel} is not in the fixture"]
    if current != original:
        return [f"workspace_unchanged: {rel} was modified or deleted"]
    return []


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _run_workspace_command(command: str, cwd: Path) -> tuple[bool, str]:
    """Run ``command`` with the shared workspace timeout. ``python`` is this interpreter."""
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
            timeout=WORKSPACE_COMMAND_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    tail = (proc.stdout + proc.stderr).strip()[-400:]
    return proc.returncode == 0, f"exit {proc.returncode}: {tail}"


def _check_files_not_touched(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "files_not_touched" not in active:
        return []
    globs = _as_str_list(active["files_not_touched"])
    if globs is None:
        return ["files_not_touched: expected a glob or list of globs"]
    touched: list[str] = []
    for call in _observed_calls(run):
        touched.extend(_path_args(call))
    hits = sorted({path for path in touched if any(fnmatch.fnmatch(path, glob) for glob in globs)})
    if hits:
        return [f"files_not_touched: matched {hits} against {globs}"]
    return []


def _check_verified_before_final(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "verified_before_final" not in active or not active["verified_before_final"]:
        return []
    flag = active["verified_before_final"]
    pattern_text = flag if isinstance(flag, str) else DEFAULT_VERIFY_REGEX
    try:
        pattern = re.compile(pattern_text)
    except re.error as exc:
        return [f"verified_before_final: invalid verify regex {pattern_text!r} ({exc})"]
    calls = _observed_calls(run)
    last_verify: int | None = None
    for index, call in enumerate(calls):
        if call.tool == "run_command" and pattern.search(_command_line(call.args)):
            last_verify = index
    if last_verify is None:
        return ["verified_before_final: no verification command was run"]
    if calls[last_verify].ok is not True:
        return ["verified_before_final: the last verification command did not pass"]
    wrote_after = [call.tool for call in calls[last_verify + 1 :] if call.tool in _WRITE_TOOLS]
    if wrote_after:
        return [
            f"verified_before_final: files were modified after the last passing verification ({wrote_after})"
        ]
    return []


def _check_history(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "history" not in active:
        return []
    spec = active["history"]
    if not isinstance(spec, dict):
        return ["history: expected a mapping of matchers"]
    unknown = sorted(set(spec) - _HISTORY_KEYS)
    if unknown:
        return [f"history: unknown matcher {unknown}"]
    failures: list[str] = []
    rows = run.history
    if "row_count" in spec:
        expected = spec["row_count"]
        if isinstance(expected, bool) or not isinstance(expected, int):
            failures.append("history: row_count must be an integer")
        elif len(rows) != expected:
            failures.append(f"history: row_count {len(rows)} != {expected}")
    if "roles" in spec:
        roles = spec["roles"]
        if not isinstance(roles, list) or not all(isinstance(item, str) for item in roles):
            failures.append("history: roles must be a list of roles")
        elif [row.role for row in rows] != list(roles):
            failures.append(f"history: roles {[row.role for row in rows]} != {list(roles)}")
    if spec.get("tool_pairs_intact") is True:
        missing_responses, missing_requests = history_orphan_ids(rows)
        if missing_responses or missing_requests:
            failures.append(
                "history: tool pairs are not intact "
                f"(requests without responses {missing_responses}, "
                f"responses without requests {missing_requests})"
            )
    if "summary_row" in spec:
        present = any(is_context_summary_row(row) for row in rows)
        if spec["summary_row"] is True and not present:
            failures.append("history: summary row is missing")
        elif spec["summary_row"] is False and present:
            failures.append("history: summary row was not expected")
    if "row_ids_preserved" in spec:
        wanted = spec["row_ids_preserved"]
        if not isinstance(wanted, list) or not all(isinstance(item, str) for item in wanted):
            failures.append("history: row_ids_preserved must be a list of ids")
        else:
            actual = [row.row_id for row in rows if row.row_id]
            cursor = 0
            for wanted_id in wanted:
                while cursor < len(actual) and actual[cursor] != wanted_id:
                    cursor += 1
                if cursor >= len(actual):
                    failures.append(f"history: row id {wanted_id!r} was not preserved")
                    break
                cursor += 1
    if spec.get("no_repaired_rows") is True:
        repaired = [index for index, row in enumerate(rows) if _is_repaired_row(row)]
        if repaired:
            failures.append(f"history: repaired rows persisted at {repaired}")
    if "blocks_include" in spec:
        failures.extend(_history_blocks(rows, spec["blocks_include"]))
    return failures


_BLOCK_SPEC_KEYS = frozenset({"type", "text_contains", "notification_type"})


def _history_blocks(rows: Sequence[Message], spec: object) -> list[str]:
    if not isinstance(spec, list):
        return ["history: blocks_include must be a list of block matchers"]
    failures: list[str] = []
    for index, item in enumerate(spec):
        if not isinstance(item, dict):
            failures.append(f"history: blocks_include[{index}] must be a mapping")
            continue
        unknown = sorted(set(item) - _BLOCK_SPEC_KEYS)
        if unknown:
            failures.append(f"history: blocks_include[{index}] unknown matcher {unknown}")
            continue
        block_type = item.get("type")
        if not isinstance(block_type, str) or not block_type:
            failures.append(f"history: blocks_include[{index}] type must be a string")
            continue
        needle = item.get("text_contains")
        if needle is not None and not isinstance(needle, str):
            failures.append(f"history: blocks_include[{index}] text_contains must be a string")
            continue
        notification = item.get("notification_type")
        if notification is not None and not isinstance(notification, str):
            failures.append(f"history: blocks_include[{index}] notification_type must be a string")
            continue
        if not any(_block_matches(block, item) for row in rows for block in row.content):
            failures.append(f"history: blocks_include[{index}] {block_type} was not kept")
    return failures


def _block_matches(block: object, spec: Mapping[str, Any]) -> bool:
    if getattr(block, "TYPE", None) != spec["type"]:
        return False
    notification = spec.get("notification_type")
    if isinstance(notification, str) and getattr(block, "notification_type", None) != notification:
        return False
    needle = spec.get("text_contains")
    if not isinstance(needle, str):
        return True
    return needle in _block_haystack(block)


def _block_haystack(block: object) -> str:
    parts: list[str] = []
    for name in ("text", "thinking", "msg", "attachment_id", "data"):
        value = getattr(block, name, None)
        if isinstance(value, str):
            parts.append(value)
    result = getattr(block, "result", None)
    if isinstance(result, list):
        parts.extend(_block_haystack(child) for child in result)
    return "\n".join(parts)


def _is_repaired_row(message: Message) -> bool:
    for block in message.content:
        if isinstance(block, ToolResponse):
            for piece in block.result:
                if not isinstance(piece, Text):
                    continue
                if piece.text in {
                    interrupted_tool_result_text(block.tool_name),
                    cancelled_tool_result_text(block.tool_name),
                }:
                    return True
        if isinstance(block, ToolRequest) and block.name == "unknown_tool" and not block.args:
            return True
    return False


def _check_store(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "store" not in active:
        return []
    spec = active["store"]
    if not isinstance(spec, dict):
        return ["store: expected a mapping of row matchers"]
    unknown = sorted(set(spec) - _STORE_KEYS)
    if unknown:
        return [f"store: unknown matcher {unknown}"]
    if run.store is None:
        return ["store: no backend snapshot was provided"]
    failures: list[str] = []
    for name in _STORE_KEYS:
        if name not in spec:
            continue
        parsed = _bounds(spec[name])
        if parsed is None:
            failures.append(f"store: {name} must be an integer or {{min, max}}")
            continue
        low, high, _exact = parsed
        message = _count_failure(f"store.{name}", getattr(run.store, name), low, high)
        if message:
            failures.append(message)
    return failures


def _check_memory_injected(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "memory_injected" not in active:
        return []
    spec = active["memory_injected"]
    if not _memory_shape(spec):
        return ["memory_injected: expected {contains: string or list of strings}"]
    assert isinstance(spec, dict)
    needles = _as_str_list(spec["contains"])
    if needles is None:
        return ["memory_injected: contains must be a string or list of strings"]
    missing = [needle for needle in needles if needle not in run.memory_text]
    if missing:
        return [f"memory_injected: missing {missing}"]
    return []


def _check_verdicts(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "verdicts" not in active:
        return []
    spec = active["verdicts"]
    if not isinstance(spec, dict):
        return ["verdicts: expected a mapping"]
    unknown = sorted(set(spec) - _VERDICT_KEYS)
    if unknown:
        return [f"verdicts: unknown matcher {unknown}"]
    verdicts = _verdicts(run)
    failures: list[str] = []
    low = spec.get("min")
    high = spec.get("max")
    if low is not None and (isinstance(low, bool) or not isinstance(low, int)):
        failures.append("verdicts: min must be an integer")
    if high is not None and (isinstance(high, bool) or not isinstance(high, int)):
        failures.append("verdicts: max must be an integer")
    if not any(
        item.startswith("verdicts: min") or item.startswith("verdicts: max") for item in failures
    ):
        message = _count_failure(
            "verdicts",
            len(verdicts),
            low if isinstance(low, int) else None,
            high if isinstance(high, int) else None,
        )
        if message:
            failures.append(message)
    if "status_in" in spec:
        allowed = _as_str_list(spec["status_in"])
        if allowed is None:
            failures.append("verdicts: status_in must be a list of statuses")
        else:
            unexpected = sorted(
                {status for status, _severity, _signals in verdicts if status not in set(allowed)}
            )
            if unexpected:
                failures.append(f"verdicts: unexpected statuses {unexpected}")
    if "severity_max" in spec:
        cap = spec["severity_max"]
        cap_rank = VERIFIER_SEVERITY_RANK.get(cap) if isinstance(cap, str) else None
        if not isinstance(cap, str) or cap_rank is None:
            failures.append(f"verdicts: unknown severity cap {cap!r}")
        else:
            over = [
                severity
                for _status, severity, _signals in verdicts
                if VERIFIER_SEVERITY_RANK.get(severity, cap_rank + 1) > cap_rank
            ]
            if over:
                failures.append(f"verdicts: {over} exceeded cap {cap!r}")
    if "signals_include" in spec:
        wanted = _as_str_list(spec["signals_include"])
        if wanted is None:
            failures.append("verdicts: signals_include must be a list of signals")
        else:
            seen = {signal for _status, _severity, signals in verdicts for signal in signals}
            missing = [signal for signal in wanted if signal not in seen]
            if missing:
                failures.append(f"verdicts: signals_include missing {missing}")
    if "signals_exclude" in spec:
        banned = _as_str_list(spec["signals_exclude"])
        if banned is None:
            failures.append("verdicts: signals_exclude must be a list of signals")
        else:
            seen = {signal for _status, _severity, signals in verdicts for signal in signals}
            hits = sorted(seen & set(banned))
            if hits:
                failures.append(f"verdicts: signals_exclude saw {hits}")
    return failures


def _check_subagent(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "subagent" not in active:
        return []
    spec = active["subagent"]
    if not isinstance(spec, dict):
        return ["subagent: expected a mapping"]
    unknown = sorted(set(spec) - _SUBAGENT_KEYS)
    if unknown:
        return [f"subagent: unknown matcher {unknown}"]
    failures: list[str] = []
    started = any(event_type(event) == "SubagentStarted" for event in _events(run))
    completed_ok = any(
        event_type(event) == "SubagentCompleted" and event_field(event, "ok") is True
        for event in _events(run)
    )
    if spec.get("started") is True and not started:
        failures.append("subagent: not started")
    if spec.get("started") is False and started:
        failures.append("subagent: started")
    if spec.get("completed_ok") is True and not completed_ok:
        failures.append("subagent: did not complete ok")
    if spec.get("completed_ok") is False and completed_ok:
        failures.append("subagent: completed ok")
    if "artifact_exists" in spec:
        paths = _as_str_list(spec["artifact_exists"])
        if paths is None:
            failures.append("subagent: artifact_exists must be a path or list of paths")
        else:
            for rel in paths:
                if not _artifact_exists(run, rel):
                    failures.append(f"subagent: artifact {rel} is missing")
    if "progress_jsonl_lines_min" in spec:
        minimum = spec["progress_jsonl_lines_min"]
        if isinstance(minimum, bool) or not isinstance(minimum, int):
            failures.append("subagent: progress_jsonl_lines_min must be an integer")
        else:
            count = _progress_lines(run)
            if count < minimum:
                failures.append(f"subagent: progress.jsonl has {count} lines < {minimum}")
    if spec.get("output_json_present") is True and not _output_json_present(run):
        failures.append("subagent: output.json is missing")
    if spec.get("output_json_present") is False and _output_json_present(run):
        failures.append("subagent: output.json was not expected")
    return failures


def _artifact_exists(run: RunView, rel: str) -> bool:
    candidates = [path / rel for path in (run.workspace, run.subagent_dir) if path is not None]
    return any(path.is_file() for path in candidates)


def _progress_lines(run: RunView) -> int:
    if run.subagent_dir is None:
        return 0
    path = run.subagent_dir / "progress.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.strip())


def _output_json_present(run: RunView) -> bool:
    return run.subagent_dir is not None and (run.subagent_dir / "output.json").is_file()


def _check_sse(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "sse" not in active:
        return []
    spec = active["sse"]
    if not isinstance(spec, dict):
        return ["sse: expected a mapping"]
    unknown = sorted(set(spec) - _SSE_KEYS)
    if unknown:
        return [f"sse: unknown matcher {unknown}"]
    frames = run.sse_frames
    failures: list[str] = []
    if spec.get("frames_have_ids") is True:
        missing = [
            index
            for index, frame in enumerate(frames)
            if event_type(frame) not in _FRAME_ID_SKIP and not _frame_id(frame)
        ]
        if missing:
            failures.append(f"sse: frames missing ids at {missing}")
    if spec.get("active_requests_once") is True:
        count = sum(1 for frame in frames if event_type(frame) == "ActiveRequests")
        if count != 1:
            failures.append(f"sse: ActiveRequests appeared {count} times")
    if spec.get("one_turn_complete_per_request") is True:
        by_request: dict[str, int] = {}
        for frame in frames:
            request_id = event_field(frame, "request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if event_type(frame) == "TurnComplete":
                by_request[request_id] = by_request.get(request_id, 0) + 1
            else:
                by_request.setdefault(request_id, 0)
        bad = sorted(request_id for request_id, count in by_request.items() if count != 1)
        if bad:
            failures.append(f"sse: requests without exactly one TurnComplete {bad}")
    if "replay_from" in spec:
        replay = spec["replay_from"]
        if not isinstance(replay, dict):
            failures.append("sse: replay_from must be a mapping")
        else:
            last_id = replay.get("last_event_id")
            expected_ids = replay.get("event_ids")
            if not isinstance(last_id, str) or not isinstance(expected_ids, list):
                failures.append("sse: replay_from needs last_event_id and event_ids")
            else:
                actual = run.sse_replay.get(last_id)
                if actual is None:
                    failures.append(f"sse: no replay from {last_id!r}")
                elif list(actual) != [item for item in expected_ids if isinstance(item, str)]:
                    failures.append(
                        f"sse: replay from {last_id!r} saw {actual}, expected {expected_ids}"
                    )
    if "golden" in spec:
        failures.extend(_check_sse_golden(spec["golden"], run))
    if spec.get("deltas_equal_final") is True:
        failures.extend(_deltas_equal_final(frames))
    return failures


def _deltas_equal_final(frames: Sequence[Any]) -> list[str]:
    """Per request, the received ``AssistantDelta`` text in order equals the settled text.

    Frames are taken as received, so a frame lost or replayed twice across a reconnect
    shows up as a mismatch.
    """
    deltas: dict[str, list[str]] = {}
    settled: dict[str, list[str]] = {}
    for frame in frames:
        request_id = event_field(frame, "request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        kind = event_type(frame)
        if kind == "AssistantDelta":
            delta = event_field(frame, "delta")
            if isinstance(delta, str):
                deltas.setdefault(request_id, []).append(delta)
        elif kind == "AssistantTextEnded":
            text = event_field(frame, "text")
            if isinstance(text, str) and text:
                settled.setdefault(request_id, []).append(text)
    if not settled:
        return ["sse: deltas_equal_final saw no AssistantTextEnded with text"]
    bad = sorted(
        request_id
        for request_id, texts in settled.items()
        if "".join(deltas.get(request_id, [])) != "".join(texts)
    )
    if bad:
        return [f"sse: concatenated AssistantDelta text differs from the final text for {bad}"]
    return []


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _check_sse_golden(spec: object, run: RunView) -> list[str]:
    if not isinstance(spec, str) or not spec.strip():
        return ["sse: golden must be a path"]
    from regression.schema.sse_norm import normalize_sse, update_golden

    path = _REPO_ROOT / spec
    normalized = normalize_sse(run.sse_wire)
    if update_golden.get():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized, encoding="utf-8")
        return []
    if not path.is_file():
        return [f"sse: missing golden {spec}; re-run with --update-golden"]
    expected = path.read_text(encoding="utf-8")
    if expected == normalized:
        return []
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            normalized.splitlines(keepends=True),
            fromfile=spec,
            tofile="actual",
            n=1,
        )
    )
    clipped = "\n".join(diff.splitlines()[:80])
    return [f"sse: golden mismatch for {spec}\n{clipped}"]


def _frame_id(frame: object) -> str:
    for name in ("id", "event_id"):
        value = event_field(frame, name)
        if isinstance(value, str) and value:
            return value
    return ""


def _check_response_contains(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "response_contains" not in active:
        return []
    phrases = _as_str_list(active["response_contains"])
    if phrases is None:
        return ["response_contains: expected a string or list of strings"]
    haystack = _response_text(run).lower()
    if phrases and not any(phrase.lower() in haystack for phrase in phrases):
        return [f"response_contains: none of {phrases} found in the agent's output"]
    return []


def _check_response_contains_all(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "response_contains_all" not in active:
        return []
    phrases = _as_str_list(active["response_contains_all"])
    if phrases is None:
        return ["response_contains_all: expected a string or list of strings"]
    haystack = _response_text(run).lower()
    missing = [phrase for phrase in phrases if phrase.lower() not in haystack]
    if missing:
        return [f"response_contains_all: missing {missing}"]
    return []


def _check_response_not_contains(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "response_not_contains" not in active:
        return []
    phrases = _as_str_list(active["response_not_contains"])
    if phrases is None:
        return ["response_not_contains: expected a string or list of strings"]
    haystack = _response_text(run).lower()
    hits = [phrase for phrase in phrases if phrase.lower() in haystack]
    if hits:
        return [f"response_not_contains: forbidden phrases {hits} found in the agent's output"]
    return []


def _check_response_regex(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "response_regex" not in active:
        return []
    patterns = _as_str_list(active["response_regex"])
    if patterns is None:
        return ["response_regex: expected a string or list of strings"]
    text = _response_text(run)
    bad: list[str] = []
    matched = False
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                matched = True
        except re.error as exc:
            bad.append(f"{pattern!r} ({exc})")
    if bad:
        return [f"response_regex: invalid pattern(s) {bad}"]
    if patterns and not matched:
        return [f"response_regex: none of {patterns} matched the agent's output"]
    return []


def _check_final_text_regex(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "final_text_regex" not in active:
        return []
    patterns = _as_str_list(active["final_text_regex"])
    if patterns is None:
        return ["final_text_regex: expected a string or list of strings"]
    text = _final_text(run)
    try:
        if patterns and not any(re.search(pattern, text) for pattern in patterns):
            return [f"final_text_regex: none of {patterns} matched the final answer"]
    except re.error as exc:
        return [f"final_text_regex: invalid pattern ({exc})"]
    return []


def _check_metrics(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "metrics" not in active:
        return []
    spec = active["metrics"]
    if not isinstance(spec, dict):
        return ["metrics: expected a mapping of score minimums"]
    failures: list[str] = []
    for name, threshold in spec.items():
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            failures.append(f"metrics: {name} threshold must be a number")
            continue
        score = run.scores.get(str(name))
        if score is None:
            failures.append(f"metrics: {name} was not scored")
        elif score < float(threshold):
            failures.append(f"metrics: {name} {score:.3f} < {float(threshold)}")
    return failures


def _check_min_score(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "min_score" not in active:
        return []
    threshold = active["min_score"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return ["min_score: expected a number"]
    if not run.scores:
        return ["min_score: no judge scores"]
    worst = min(run.scores.values())
    if worst < float(threshold):
        return [f"min_score: worst judge score {worst:.3f} < {float(threshold)}"]
    return []


def _check_min_summarizations(active: Mapping[str, Any], run: RunView) -> list[str]:
    if "min_summarizations" not in active:
        return []
    minimum = active["min_summarizations"]
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        return ["min_summarizations: expected an integer"]
    count = sum(1 for event in _events(run) if event_type(event) == "ContextSummarized")
    if count < minimum:
        return [f"min_summarizations: {count} < {minimum}"]
    return []


def evaluate_live_extensions(assertions: Mapping[str, Any], run: RunView) -> list[str]:
    """Checks for keys the live runner records. Other keys stay with that runner."""
    failures: list[str] = []
    for check in (
        _check_tool_args,
        _check_per_session,
        _check_evidence_paths,
        _check_subagent_manifest,
        _check_system_prompt,
        _check_chat_history,
        _check_sse,
    ):
        failures.extend(check(assertions, run))
    return failures


def _tool_arg_maps(run: RunView) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in _events(run):
        if event_type(event) != "ToolCallStarted":
            continue
        tool = event_field(event, "tool")
        args = event_field(event, "args")
        if not isinstance(tool, str) or not tool or not isinstance(args, dict):
            continue
        grouped.setdefault(tool, []).append(dict(args))
    return grouped


def _has_stop_guard(args: Mapping[str, Any]) -> bool:
    """A loop stop guard is max_ticks, a max runtime, or unbounded=true."""
    max_ticks = args.get("max_ticks")
    if isinstance(max_ticks, int) and not isinstance(max_ticks, bool) and max_ticks >= 1:
        return True
    max_runtime = args.get("max_runtime")
    if isinstance(max_runtime, str) and max_runtime.strip():
        return True
    if (
        isinstance(max_runtime, (int, float))
        and not isinstance(max_runtime, bool)
        and max_runtime > 0
    ):
        return True
    return args.get("unbounded") is True


def _command_blob(args: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("command", "cmd", "shell", "script"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            parts.append(raw)
    for key in ("argv", "args", "arguments"):
        raw = args.get(key)
        if isinstance(raw, list):
            parts.extend(str(item) for item in raw if item is not None)
    return " ".join(parts)


def _check_tool_args(active: Mapping[str, Any], run: RunView) -> list[str]:
    spec = active.get("tool_args")
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return ["tool_args: expected a mapping of tool name to matchers"]
    calls = _tool_arg_maps(run)
    failures: list[str] = []
    for tool, matchers in spec.items():
        label = f"tool_args.{tool}"
        if not isinstance(matchers, dict):
            failures.append(f"{label}: expected a mapping of matchers")
            continue
        found = calls.get(str(tool), [])
        if not found:
            failures.append(f"{label}: tool was not called")
            continue
        if matchers.get("stop_guard") is True and not any(_has_stop_guard(args) for args in found):
            failures.append(
                f"{label}: no call had a stop guard (max_ticks, max_runtime, or unbounded)"
            )
        needles = matchers.get("command_contains")
        if needles is None:
            continue
        if isinstance(needles, str):
            needles = [needles]
        if not isinstance(needles, list) or not all(isinstance(item, str) for item in needles):
            failures.append(f"{label}: command_contains expected a string or list of strings")
            continue
        if not any(
            all(needle.lower() in _command_blob(args).lower() for needle in needles)
            for args in found
        ):
            failures.append(f"{label}: no call contained {needles}")
    return failures


def _check_per_session(active: Mapping[str, Any], run: RunView) -> list[str]:
    spec = active.get("per_session")
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return ["per_session: expected a mapping of session number to assertions"]
    nested = (
        _check_tool_args,
        _check_required_tools,
        _check_response_contains,
        _check_response_contains_all,
        _check_response_not_contains,
        _check_response_regex,
    )
    failures: list[str] = []
    for number, block in spec.items():
        where = f"per_session.{number}"
        if not isinstance(block, dict):
            failures.append(f"{where}: expected a mapping")
            continue
        try:
            session = int(number)
        except (TypeError, ValueError):
            failures.append(f"{where}: session was not recorded")
            continue
        events = run.session_events.get(session)
        if events is None:
            failures.append(f"{where}: session was not recorded")
            continue
        scoped = replace(run, events=list(events))
        for check in nested:
            for failure in check(block, scoped):
                failures.append(f"{where}: {failure}")
    return failures


def _read_paths(run: RunView) -> set[str]:
    found: set[str] = set()
    for event in _events(run):
        if event_type(event) != "ToolCallStarted" or event_field(event, "tool") != "read_file":
            continue
        args = event_field(event, "args")
        if not isinstance(args, dict):
            continue
        for key in ("path", "file_path", "file"):
            raw = args.get(key)
            if isinstance(raw, str) and raw.strip():
                found.add(_norm_path(raw))
    return found


def _cited_was_read(cited: str, read_paths: set[str]) -> bool:
    norm = _norm_path(cited)
    if not norm:
        return False
    if norm in read_paths:
        return True
    return any(
        read == norm or read.endswith("/" + norm) or norm.endswith("/" + read)
        for read in read_paths
    )


def _cited_exists(cited: str, workspace: Path) -> bool:
    norm = _norm_path(cited)
    if not norm:
        return False
    root = workspace.resolve()
    candidate = (root / norm).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_file()


def _check_evidence_paths(active: Mapping[str, Any], run: RunView) -> list[str]:
    spec = active.get("evidence_paths")
    if spec is None:
        return []
    cited = exist = was_read = True
    if spec is True:
        pass
    elif isinstance(spec, dict):
        cited = bool(spec.get("cited", True))
        exist = bool(spec.get("exist", True))
        was_read = bool(spec.get("read", True))
    else:
        return ["evidence_paths: expected true or a mapping"]
    paths = extract_evidence_paths(_response_text(run))
    failures: list[str] = []
    if cited and not paths:
        failures.append("evidence_paths: no Evidence paths were cited")
    if not paths:
        return failures
    read_paths = _read_paths(run)
    for path in paths:
        if exist:
            if run.workspace is None:
                failures.append(f"evidence_paths: {path} was cited but no workspace was recorded")
            elif not _cited_exists(path, run.workspace):
                failures.append(f"evidence_paths: {path} was cited but does not exist")
        if was_read and not _cited_was_read(path, read_paths):
            failures.append(f"evidence_paths: {path} was cited but was not read")
    return failures


def _check_subagent_manifest(active: Mapping[str, Any], run: RunView) -> list[str]:
    spec = active.get("subagent_manifest")
    if spec is None:
        return []
    if not isinstance(spec, dict) or not isinstance(spec.get("subagent_type"), str):
        return ["subagent_manifest: expected subagent_type"]
    wanted = spec["subagent_type"]
    found = [
        value
        for manifest in run.manifests
        if isinstance(value := manifest.get("subagent_type"), str) and value
    ]
    if wanted not in found:
        shown = found or "no child manifests"
        return [f"subagent_manifest: subagent_type {wanted!r} not in {shown}"]
    return []


def _check_system_prompt(active: Mapping[str, Any], run: RunView) -> list[str]:
    spec = active.get("system_prompt")
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return ["system_prompt: expected a mapping"]
    phrases = spec.get("last_not_contains") or []
    if isinstance(phrases, str):
        phrases = [phrases]
    if not isinstance(phrases, list) or not all(isinstance(item, str) for item in phrases):
        return ["system_prompt: last_not_contains expected a string or list of strings"]
    if not run.system_prompts or not str(run.system_prompts[-1]).strip():
        return ["system_prompt: no system prompt was recorded for the last turn"]
    last = run.system_prompts[-1]
    hits = [phrase for phrase in phrases if phrase in last]
    if hits:
        return [f"system_prompt: last turn contains {hits}"]
    return []


def _history_text(row: Mapping[str, Any]) -> str:
    text = row.get("text")
    if isinstance(text, str):
        return text
    content = row.get("content")
    return content if isinstance(content, str) else ""


def _check_chat_history(active: Mapping[str, Any], run: RunView) -> list[str]:
    spec = active.get("chat_history")
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return ["chat_history: expected a mapping"]
    if not run.chat_history:
        return ["chat_history: no history snapshot was recorded"]
    snapshot = run.chat_history[-1]
    users = [row for row in snapshot if isinstance(row, dict) and row.get("role") == "user"]
    assistants = [
        row
        for row in snapshot
        if isinstance(row, dict) and row.get("role") == "assistant" and _history_text(row).strip()
    ]
    failures: list[str] = []
    if spec.get("one_user_message") is True and len(users) != 1:
        failures.append(f"chat_history: expected one user message, saw {len(users)}")
    if spec.get("user_equals_turn_input") is True:
        expected = run.turn_inputs[-1] if run.turn_inputs else ""
        actual = _history_text(users[-1]) if users else ""
        if actual != expected:
            failures.append("chat_history: user message does not equal the tick prompt")
    if spec.get("assistant_nonempty") is True and not assistants:
        failures.append("chat_history: no assistant reply")
    return failures


def _check_budgets(active: Mapping[str, Any], run: RunView) -> list[str]:
    totals = _usage_totals(run)
    checks = (
        ("max_latency_ms", "duration_ms"),
        ("max_input_tokens", "input_tokens"),
        ("max_output_tokens", "output_tokens"),
        ("max_cost_usd", "cost_usd"),
    )
    failures: list[str] = []
    for key, source in checks:
        if key not in active:
            continue
        limit = active[key]
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            failures.append(f"{key}: expected a number")
            continue
        actual = totals[source]
        if actual > float(limit):
            failures.append(f"{key}: {actual} > {limit}")
    return failures


__all__ = [
    "DEFAULT_VERIFY_REGEX",
    "WORKSPACE_COMMAND_TIMEOUT_SEC",
    "ObservedCall",
    "ProviderRequest",
    "RunView",
    "StoreView",
    "canonical_target",
    "evaluate",
    "evaluate_live_extensions",
    "event_field",
    "event_type",
    "history_orphan_ids",
    "tool_category",
    "validate_assertions",
]
