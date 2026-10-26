"""T1-GW-01..04. Schema of event_to_json, lossy fields, framing, verdict wire."""

from __future__ import annotations

import asyncio
import json
import types
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import jsonschema
import pytest

from monkeybot.core.runtime.events import (
    AgentEvent,
    AssistantDelta,
    FileBlock,
    ImageBlock,
    SystemContextUpdated,
    Thinking,
    ToolCallResult,
    ToolCallStarted,
    TurnComplete,
    UsageTotals,
    VerifierVerdict,
    event_to_json,
)
from testkit.gateway import open_gateway
from testkit.sse import SseFrame, SseParser

_GOLDEN = Path(__file__).resolve().parent / "goldens" / "sse" / "schema.json"
_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
    "estimated_prompt_tokens",
)
_KIND_ORDER = (
    "Thinking",
    "AssistantDelta",
    "AssistantTextStarted",
    "AssistantTextEnded",
    "ToolInputDelta",
    "ToolCallStarted",
    "ToolCallResult",
    "TurnComplete",
    "Error",
    "ContextSummarizing",
    "ContextSummarized",
    "ContextUsage",
    "ContextEpochStarted",
    "SystemContextUpdated",
    "SystemPromptSnapshot",
    "ImageBlock",
    "FileBlock",
    "ThinkingBlockStarted",
    "ThinkingBlockDelta",
    "ThinkingBlockComplete",
    "RedactedThinkingBlock",
    "ToolConfirmationRequest",
    "AskUserRequest",
    "ActionRequiredEvent",
    "FrontendToolRequest",
    "SystemNotificationEvent",
    "AttachmentDescriptor",
    "GroundingEvent",
    "UserSteered",
    "QueuedInputAccepted",
    "ConfigReloaded",
    "SubagentStarted",
    "SubagentEvent",
    "SubagentCompleted",
    "CredentialEgressBlocked",
    "VerifierVerdict",
    "HistoryRewritten",
    "ActiveRequests",
)
_BASIC_SCRIPT = """
- - type: text
    text: hello
  - type: done
"""


def _kind_of(cls: type) -> str:
    default = cls.__dataclass_fields__["kind"].default
    if not isinstance(default, str):
        raise TypeError(f"{cls.__name__} has no kind default")
    return default


def _value(annotation: Any, *, full: bool, name: str) -> Any:
    if name == "inner":
        return AssistantDelta(request_id="r", delta="d")
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is str:
        return "sample" if full else ""
    if annotation is int:
        return 1 if full else 0
    if annotation is float:
        return 1.5 if full else 0.0
    if annotation is bool:
        return bool(full)
    if origin is Literal:
        return args[-1] if full else args[0]
    if origin in (Union, types.UnionType):
        non_none = [item for item in args if item is not type(None)]
        if not full and type(None) in args:
            return None
        return _value(non_none[0], full=full, name=name)
    if origin is dict:
        return {"k": "v"} if full else {}
    if origin is list:
        inner = args[0] if args else str
        return [_value(inner, full=True, name=name)] if full else []
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return (_value(args[0], full=True, name=name),) if full else ()
        return tuple(_value(item, full=True, name=name) for item in args) if full else ()
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _build(annotation, full=full)
    raise TypeError(f"cannot sample {name}: {annotation!r}")


def _build(cls: type, *, full: bool) -> Any:
    if not full:
        if _kind_of(cls) == "SubagentEvent":
            return cls(inner=Thinking())
        return cls()
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for field in fields(cls):
        if field.name == "kind":
            continue
        kwargs[field.name] = _value(hints[field.name], full=True, name=field.name)
    return cls(**kwargs)


def _samples() -> list[AgentEvent]:
    found: list[AgentEvent] = []
    for cls in get_args(AgentEvent):
        found.append(_build(cls, full=False))
        found.append(_build(cls, full=True))
    found.append(
        ImageBlock(
            request_id="r",
            image_id="img",
            mime_type="image/png",
            data="abcd",
            path="",
        )
    )
    found.append(
        ImageBlock(
            request_id="r",
            image_id="img",
            mime_type="image/png",
            data="nope",
            path="a.png",
        )
    )
    found.append(
        FileBlock(
            request_id="r",
            file_id="f",
            mime_type="application/pdf",
            data="AAAA",
            path="a.pdf",
            filename="a.pdf",
        )
    )
    return found


def _payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in _samples():
        parsed = json.loads(event_to_json(event))
        if not isinstance(parsed, dict):
            raise TypeError(f"{type(event).__name__} did not serialize to an object")
        payloads.append(parsed)
    return payloads


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(type(value))


def _merge_types(types: set[str]) -> Any:
    present = set(types)
    if "integer" in present and "number" in present:
        present.discard("integer")
    if present == {"null"}:
        return "null"
    non_null = sorted(present - {"null"})
    if not non_null:
        return "null"
    if len(non_null) > 1:
        raise TypeError(f"incompatible JSON types {sorted(present)}")
    kind = non_null[0]
    if "null" in present:
        return [kind, "null"]
    return kind


def _order_keys(keys: set[str]) -> list[str]:
    head = [key for key in ("type", "request_id") if key in keys]
    return head + sorted(key for key in keys if key not in head)


def _closed_object(samples: list[dict[str, Any]]) -> dict[str, Any]:
    keys: set[str] = set()
    for sample in samples:
        keys.update(sample)
    required = [key for key in _order_keys(keys) if all(key in sample for sample in samples)]
    properties: dict[str, Any] = {}
    for key in _order_keys(keys):
        values = [sample[key] for sample in samples if key in sample]
        properties[key] = _infer(key, values)
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _infer(key: str, values: list[Any]) -> dict[str, Any]:
    if key == "type" and values and all(isinstance(item, str) for item in values):
        kinds = set(values)
        if len(kinds) == 1:
            return {"const": values[0]}
    if key == "inner":
        return {"$ref": "#/$defs/event"}
    if key == "usage":
        objects = [item for item in values if isinstance(item, dict)]
        return _closed_object(objects)
    types = {_json_type(item) for item in values}
    if types <= {"object", "null"} and any(isinstance(item, dict) for item in values):
        schema: dict[str, Any] = {"type": "object", "additionalProperties": True}
        if "null" in types:
            schema = {"type": ["object", "null"], "additionalProperties": True}
        return schema
    if "array" in types:
        elements: list[Any] = []
        for item in values:
            if isinstance(item, list):
                elements.extend(item)
        item_schema: dict[str, Any]
        if not elements:
            item_schema = {}
        elif all(isinstance(item, str) for item in elements):
            item_schema = {"type": "string"}
        elif all(isinstance(item, dict) for item in elements):
            item_schema = {"type": "object", "additionalProperties": True}
        else:
            item_schema = {}
        array_type = _merge_types(types)
        return {"type": array_type, "items": item_schema}
    return {"type": _merge_types(types)}


def _active_requests() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "request_ids"],
        "properties": {
            "type": {"const": "ActiveRequests"},
            "request_ids": {"type": "array", "items": {"type": "string"}},
        },
    }


def build_schema() -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for payload in _payloads():
        kind = payload.get("type")
        if not isinstance(kind, str):
            raise TypeError("event JSON is missing type")
        grouped.setdefault(kind, []).append(payload)
    grouped["ActiveRequests"] = [{"type": "ActiveRequests", "request_ids": []}]
    known = [kind for kind in _KIND_ORDER if kind in grouped]
    extra = sorted(set(grouped) - set(known))
    order = known + extra
    defs: dict[str, Any] = {
        "event": {"oneOf": [{"$ref": f"#/$defs/{kind}"} for kind in order]},
    }
    for kind in order:
        if kind == "ActiveRequests":
            defs[kind] = _active_requests()
        else:
            defs[kind] = _closed_object(grouped[kind])
    return {
        "$schema": _SCHEMA_DIALECT,
        "$defs": defs,
        "$ref": "#/$defs/event",
    }


def _schema_text(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def load_schema(request: pytest.FixtureRequest) -> dict[str, Any]:
    schema = build_schema()
    text = _schema_text(schema)
    update = bool(request.config.getoption("update_golden"))
    if update:
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(text, encoding="utf-8")
        return schema
    assert _GOLDEN.is_file(), "missing sse/schema.json; re-run with --update-golden"
    assert _GOLDEN.read_text(encoding="utf-8") == text
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _validator(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _keys(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(key)
            found.extend(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_keys(item))
    return found


def _wire(frame: SseFrame) -> str:
    lines: list[str] = []
    if frame.id is not None:
        lines.append(f"id: {frame.id}")
    if frame.data is not None:
        lines.extend(f"data: {part}" for part in frame.data.split("\n"))
    if frame.comment is not None:
        lines.extend(f": {part}" for part in frame.comment.split("\n"))
    return "\n".join(lines) + "\n\n"


def _kind(frame: SseFrame) -> str | None:
    if frame.data is None:
        if frame.comment and frame.comment.startswith("ping"):
            return "ping"
        return None
    try:
        payload = json.loads(frame.data)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("type"), str):
        return payload["type"]
    return None


@pytest.mark.case("T1-GW-01")
def test_every_kind(request: pytest.FixtureRequest) -> None:
    """Every AgentEvent kind validates; no chat_request_id; usage has 8 keys."""
    schema = load_schema(request)
    validator = _validator(schema)
    payloads = _payloads()
    kinds = {_kind_of(cls) for cls in get_args(AgentEvent)}
    seen = {payload["type"] for payload in payloads}
    assert seen == kinds
    one_of = schema["$defs"]["event"]["oneOf"]
    named = {item["$ref"].rsplit("/", 1)[-1] for item in one_of}
    assert kinds <= named
    assert "ActiveRequests" in named
    for payload in payloads:
        validator.validate(payload)
        assert "chat_request_id" not in _keys(payload)
    usage = json.loads(event_to_json(TurnComplete(request_id="r", usage=UsageTotals())))["usage"]
    assert set(usage) == set(_USAGE_KEYS)
    assert len(usage) == 8
    usage_schema = schema["$defs"]["TurnComplete"]["properties"]["usage"]
    assert usage_schema["additionalProperties"] is False
    assert set(usage_schema["required"]) == set(_USAGE_KEYS)
    poisoned = json.loads(event_to_json(Thinking(request_id="r")))
    poisoned["chat_request_id"] = "r"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(poisoned)
    validator.validate({"type": "ActiveRequests", "request_ids": ["r1"]})


@pytest.mark.case("T1-GW-02")
def test_lossy_fields_pinned(request: pytest.FixtureRequest) -> None:
    """Omitted wire fields stay omitted, and the schema rejects them."""
    schema = load_schema(request)
    validator = _validator(schema)
    started = json.loads(
        event_to_json(
            ToolCallStarted(
                request_id="r",
                tool="read_file",
                label="read_file",
                args={"path": "a.txt"},
                call_id="c1",
                inspector_decision="deny",
                resource="file",
                resolved_path="/tmp/a.txt",
            )
        )
    )
    for key in ("inspector_decision", "resource", "resolved_path"):
        assert key not in started
    result = json.loads(
        event_to_json(
            ToolCallResult(
                request_id="r",
                tool="read_file",
                result="ok",
                call_id="c1",
                error_kind="policy",
                duration_ms=12,
            )
        )
    )
    assert "error_kind" not in result
    assert "duration_ms" not in result
    file_block = json.loads(
        event_to_json(
            FileBlock(
                request_id="r",
                file_id="f",
                mime_type="application/pdf",
                data="AAAA",
                path="a.pdf",
                filename="a.pdf",
            )
        )
    )
    assert "data" not in file_block
    updated = json.loads(
        event_to_json(
            SystemContextUpdated(
                request_id="r",
                epoch_id=1,
                changed_sources=["current_date"],
                text="secret body",
            )
        )
    )
    assert "text" not in updated
    for payload, key, value in (
        (started, "inspector_decision", "deny"),
        (started, "resource", "file"),
        (started, "resolved_path", "/tmp/a.txt"),
        (result, "error_kind", "policy"),
        (result, "duration_ms", 12),
        (file_block, "data", "AAAA"),
        (updated, "text", "secret body"),
    ):
        rejected = dict(payload)
        rejected[key] = value
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(rejected)


@pytest.mark.case("T1-GW-03")
async def test_framing() -> None:
    """ActiveRequests and pings have no id; a garbage Last-Event-ID replays everything."""
    frames: list[SseFrame] = []
    async with open_gateway(script=_BASIC_SCRIPT) as gateway:
        await gateway.create_session("s1")
        async with gateway.stream_events("s1") as stream:
            parser = SseParser()
            posted = False
            saw_ping = False
            async for chunk in stream:
                for frame in parser.feed(chunk):
                    frames.append(frame)
                    if not posted and _kind(frame) == "ActiveRequests":
                        response = await gateway.post_reply("s1", "r1", "Say hello")
                        assert response.status_code == 200
                        posted = True
                    if _kind(frame) == "ping":
                        saw_ping = True
                if saw_ping and any(_kind(frame) == "TurnComplete" for frame in frames):
                    break
            if not saw_ping:
                extra = parser.feed(await _next_chunk(stream, timeout=2.0))
                frames.extend(extra)
                saw_ping = any(_kind(frame) == "ping" for frame in frames)
        assert saw_ping
        bare = [frame for frame in frames if _kind(frame) in {"ActiveRequests", "ping"}]
        assert bare
        for frame in bare:
            assert frame.id is None
            assert not _wire(frame).startswith("id:")
        original_ids = [frame.id for frame in frames if frame.id is not None]
        assert original_ids
        replayed: list[str] = []
        async with gateway.stream_events("s1", last_event_id="garbage") as stream:
            parser = SseParser()
            async for chunk in stream:
                done = False
                for frame in parser.feed(chunk):
                    if _kind(frame) == "ActiveRequests":
                        assert frame.id is None
                        done = True
                        break
                    if frame.id is not None:
                        replayed.append(frame.id)
                if done:
                    break
        assert replayed == original_ids


async def _next_chunk(stream: Any, *, timeout: float) -> bytes:
    return await asyncio.wait_for(stream.__anext__(), timeout=timeout)


@pytest.mark.case("T1-GW-04")
def test_verdict_wire_snake_case(request: pytest.FixtureRequest) -> None:
    """SSE VerifierVerdict is snake_case, not to_wire() camelCase."""
    schema = load_schema(request)
    validator = _validator(schema)
    event = VerifierVerdict(
        request_id="r",
        verdict_id="v1",
        checkpoint_id="c1",
        status="on_track",
        severity="none",
        confidence=0.5,
        rationale="steady",
        correction="none needed",
        triggering_signals=("loop",),
        triggering_signal_epochs=(("loop", 1),),
        judge_tokens=3,
    )
    payload = json.loads(event_to_json(event))
    camel = event.to_wire()
    assert "verdict_id" in payload
    assert "checkpoint_id" in payload
    assert "triggering_signals" in payload
    assert "triggering_signal_epochs" in payload
    assert "judge_tokens" in payload
    assert "verdictId" not in payload
    assert "verdictId" in camel
    assert "checkpointId" in camel
    assert "triggeringSignals" in camel
    validator.validate(payload)
    camel_body = {"type": "VerifierVerdict", "request_id": "r", **camel}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(camel_body)
