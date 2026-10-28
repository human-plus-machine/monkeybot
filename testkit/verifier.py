"""Verifier doubles shared by goal-ledger tests and scripted T2 scenarios.

Scenario YAML carries one classifier row per human message in
``config.env.MONKEYBOT_CLASSIFIER_ROWS`` and judge completions in
``config.env.MONKEYBOT_JUDGE_SCRIPT``. Both values are JSON strings.
``done_when`` is stored on the classification and persisted with the goal row.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.llm.provider import Done, Message, TextDelta, UsageEvent
from monkeybot.core.persistence.goal_ledger import (
    Classification,
    ConstraintDraft,
    ConstraintKind,
    GoalEntry,
    Intent,
)
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.verifier.mailbox import VerdictMailbox

CLASSIFIER_ROWS_ENV = "MONKEYBOT_CLASSIFIER_ROWS"
JUDGE_SCRIPT_ENV = "MONKEYBOT_JUDGE_SCRIPT"
_ROW_KEYS = frozenset({"intent", "relates_to", "constraints", "done_when"})
_JUDGE_MODES = frozenset({"valid", "malformed", "timeout"})


def _done_when(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ValueError("done_when must be a list of non-empty strings")
    return tuple(item.strip() for item in raw)


def _constraint(raw: object) -> ConstraintDraft:
    if not isinstance(raw, Mapping):
        raise ValueError("classifier constraint must be a mapping")
    unknown = sorted(set(raw) - {"kind", "pattern", "verbatim"})
    if unknown:
        raise ValueError(f"unknown classifier constraint keys: {unknown}")
    kind_raw = str(raw.get("kind") or "").strip()
    try:
        kind = ConstraintKind(kind_raw)
    except ValueError as exc:
        raise ValueError(f"unknown constraint kind {kind_raw!r}") from exc
    pattern = str(raw.get("pattern") or "").strip()
    verbatim = str(raw.get("verbatim") or "").strip()
    if not pattern and not verbatim:
        raise ValueError("classifier constraint needs pattern or verbatim")
    return ConstraintDraft(
        kind=kind,
        pattern=pattern or verbatim,
        verbatim=verbatim or pattern,
    )


def _classification(
    raw: Classification | Mapping[str, Any],
) -> tuple[Classification, tuple[str, ...]]:
    if isinstance(raw, Classification):
        return raw, raw.done_when
    if not isinstance(raw, Mapping):
        raise ValueError("classifier row must be a Classification or a mapping")
    unknown = sorted(set(raw) - _ROW_KEYS)
    if unknown:
        raise ValueError(f"unknown classifier row keys: {unknown}")
    intent_raw = str(raw.get("intent") or "").strip()
    try:
        intent = Intent(intent_raw)
    except ValueError as exc:
        raise ValueError(f"unknown classifier intent {intent_raw!r}") from exc
    relates = raw.get("relates_to")
    relates_to = str(relates).strip() if isinstance(relates, str) else None
    if relates_to in ("", "null", "None"):
        relates_to = None
    raw_constraints = raw.get("constraints") or []
    if not isinstance(raw_constraints, list):
        raise ValueError("classifier constraints must be a list")
    constraints = tuple(_constraint(item) for item in raw_constraints)
    done_when = _done_when(raw.get("done_when"))
    return (
        Classification(
            intent=intent,
            relates_to=relates_to,
            constraints=constraints,
            done_when=done_when,
        ),
        done_when,
    )


def _rows_payload(raw: str) -> list[object]:
    payload = json.loads(raw)
    if isinstance(payload, dict) and set(payload) == {"rows"}:
        payload = payload["rows"]
    if not isinstance(payload, list):
        raise ValueError("classifier rows must be a JSON list")
    return payload


class ScriptedClassifier:
    """Queued classifications. YAML rows are one intent plus constraints per human message."""

    def __init__(
        self,
        results: Sequence[Classification | Mapping[str, Any]],
        *,
        delay_s: float = 0.0,
    ) -> None:
        parsed = [_classification(item) for item in results]
        self._results = [item[0] for item in parsed]
        # Parallel to the original rows. The ledger does not read this.
        self.done_when: list[tuple[str, ...]] = [item[1] for item in parsed]
        self._delay_s = delay_s
        self.calls: list[str] = []

    @classmethod
    def from_json(cls, raw: str, *, delay_s: float = 0.0) -> ScriptedClassifier:
        rows = _rows_payload(raw)
        typed: list[Classification | Mapping[str, Any]] = []
        for item in rows:
            if not isinstance(item, Mapping):
                raise ValueError("classifier row must be a JSON object")
            typed.append(dict(item))
        return cls(typed, delay_s=delay_s)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        delay_s: float = 0.0,
    ) -> ScriptedClassifier | None:
        """Build from ``MONKEYBOT_CLASSIFIER_ROWS``, or None when it is unset."""
        source = os.environ if environ is None else environ
        raw = source.get(CLASSIFIER_ROWS_ENV, "").strip()
        if not raw:
            return None
        return cls.from_json(raw, delay_s=delay_s)

    async def classify(
        self,
        verbatim: str,
        open_entries: Sequence[GoalEntry],
        *,
        thread_id: str = "",
    ) -> Classification:
        del open_entries, thread_id
        self.calls.append(verbatim)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        if not self._results:
            return Classification(intent=Intent.NOISE, relates_to=None, constraints=())
        return self._results.pop(0)


def _judge_completion(raw: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, str):
        mode = "valid" if raw.lstrip().startswith("{") else "malformed"
        return {"mode": mode, "body": raw, "hang_s": 0.0, "input_tokens": 0, "output_tokens": 0}
    if not isinstance(raw, Mapping):
        raise ValueError("judge completion must be a string or a mapping")
    unknown = sorted(set(raw) - {"mode", "body", "hang_s", "input_tokens", "output_tokens"})
    if unknown:
        raise ValueError(f"unknown judge completion keys: {unknown}")
    mode = str(raw.get("mode") or "").strip()
    if mode not in _JUDGE_MODES:
        raise ValueError(f"judge completion mode must be one of {sorted(_JUDGE_MODES)}")
    hang = raw.get("hang_s") or 0
    if isinstance(hang, bool) or not isinstance(hang, (int, float)) or hang < 0:
        raise ValueError("judge hang_s must be a non-negative number")
    body = raw.get("body") or ""
    if not isinstance(body, str):
        raise ValueError("judge completion body must be a string")
    tokens: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens"):
        value = raw.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"judge {key} must be a non-negative integer")
        tokens[key] = value
    if mode == "valid" and not body.strip():
        raise ValueError("valid judge completion needs a JSON body")
    return {"mode": mode, "body": body, "hang_s": float(hang), **tokens}


def _judge_payload(raw: str) -> list[object]:
    payload = json.loads(raw)
    if isinstance(payload, dict) and "completions" in payload:
        completions = payload["completions"]
    else:
        completions = payload
    if isinstance(completions, dict):
        completions = [completions]
    if not isinstance(completions, list):
        raise ValueError("judge script must be a completion or a list of completions")
    return completions


class ScriptedJudge:
    """Fixed judge completions for ``ProviderJudge``: valid JSON, malformed text, or timeout.

    ``timeout`` raises ``TimeoutError`` (after ``hang_s``, when set) so
    ``ProviderJudge`` fail-opens. ``hang_s`` on ``valid`` or ``malformed`` delays
    that reply so a later checkpoint can land first.
    """

    def __init__(self, completions: Sequence[Mapping[str, Any] | str] | None = None) -> None:
        self._completions = [_judge_completion(item) for item in (completions or ())]
        self.calls: list[str] = []
        self.bodies: list[str] = []

    @classmethod
    def from_json(cls, raw: str) -> ScriptedJudge:
        items: list[Mapping[str, Any] | str] = []
        for item in _judge_payload(raw):
            if isinstance(item, str):
                items.append(item)
                continue
            if not isinstance(item, Mapping):
                raise ValueError("judge completion must be a JSON object or string")
            items.append(dict(item))
        return cls(items)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ScriptedJudge | None:
        """Build from ``MONKEYBOT_JUDGE_SCRIPT``, or None when it is unset."""
        source = os.environ if environ is None else environ
        raw = source.get(JUDGE_SCRIPT_ENV, "").strip()
        if not raw:
            return None
        return cls.from_json(raw)

    @property
    def name(self) -> str:
        return "scripted-judge"

    @property
    def supports_streaming(self) -> bool:
        return True

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> Any:
        del messages, tools, thinking_budget
        return self._stream(model)

    async def _stream(self, model: str) -> Any:
        self.calls.append(model)
        if not self._completions:
            raise RuntimeError("scripted judge has no completion left")
        item = self._completions.pop(0)
        hang = float(item["hang_s"])
        if item["mode"] == "timeout":
            if hang > 0:
                await asyncio.sleep(hang)
            raise TimeoutError("scripted judge timeout")
        if hang > 0:
            await asyncio.sleep(hang)
        body = str(item["body"])
        if item["mode"] == "malformed" and not body:
            body = "not json"
        self.bodies.append(body)
        yield TextDelta(text=body)
        in_tokens = int(item["input_tokens"])
        out_tokens = int(item["output_tokens"])
        if in_tokens or out_tokens:
            yield UsageEvent(input_tokens=in_tokens, output_tokens=out_tokens)
        yield Done()

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
    ) -> int:
        del messages, tools, model, thinking_budget
        return 0


def _mailbox(thread_id: str = "t1", request_id: str = "r1") -> VerdictMailbox:
    mailbox = VerdictMailbox()
    mailbox.open_request(thread_id, request_id)
    return mailbox


def _tracker_ctx() -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("write_file", "w", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


def verifier_payload(
    *,
    event: HookEvent,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_error: str | None = None,
    inner_turn: int = 3,
    assistant_text: str | None = None,
    tool_requests: list[Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> HookPayload:
    return HookPayload(
        event=event,
        thread_id="t1",
        request_id="r1",
        ctx=_tracker_ctx(),
        tool_name=tool_name,
        tool_args=tool_args,
        tool_error=tool_error,
        inner_turn=inner_turn,
        assistant_text=assistant_text,
        tool_requests=tool_requests,
        usage=usage,
    )
