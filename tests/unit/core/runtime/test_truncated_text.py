"""A text answer cut off at the output limit is continued, then marked if it still ends there."""

from __future__ import annotations

from typing import Any

import pytest

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.llm.provider import Done, Message, TextDelta
from monkeybot.core.runtime.events import HarnessIntervention, SystemNotificationEvent
from monkeybot.core.runtime.loop import run
from monkeybot.core.runtime.provider_retry import RESUME_NOTE
from monkeybot.core.runtime.turn_loop import TRUNCATED_ANSWER_NOTICE
from monkeybot.core.types.content_blocks import Text
from testkit.context import _ctx
from testkit.executors import RecordingExecutor
from testkit.history import FakeHistory
from testkit.inspectors import AllowInspector
from testkit.provider import FakeProvider


def _script(*turns: tuple[str, bool]) -> list[list[object]]:
    return [[TextDelta(text=text), Done(truncated=truncated)] for text, truncated in turns]


def _assistant_texts(hist: FakeHistory) -> list[str]:
    texts: list[str] = []
    for message in hist.rows:
        if message.role != "assistant":
            continue
        texts.extend(block.text for block in message.content if isinstance(block, Text))
    return texts


def _interventions(events: list[Any]) -> list[tuple[str, str]]:
    return [
        (event.intervention, event.detail)
        for event in events
        if isinstance(event, HarnessIntervention) and event.intervention == "truncated_text"
    ]


def _notices(events: list[Any], kind: str) -> list[SystemNotificationEvent]:
    return [
        event
        for event in events
        if isinstance(event, SystemNotificationEvent) and event.notification_type == kind
    ]


def _message_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


async def _run(
    script: list[list[object]],
    *,
    max_turns: int = 6,
    hook_manager: HookManager | None = None,
    outputs: list[str] | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[list[Any], FakeHistory, FakeProvider]:
    if outputs is not None and monkeypatch is not None:

        def _capture(*_args: object, output_value: str | None = None, **_kwargs: object) -> None:
            if output_value:
                outputs.append(output_value)

        monkeypatch.setattr("monkeybot.observability.spans.set_turn_io", _capture)
    hist = FakeHistory()
    prov = FakeProvider(script)
    events = [
        event
        async for event in run(
            "write the answer",
            _ctx(),
            provider=prov,
            history=hist,
            inspectors=[AllowInspector()],
            tool_executor=RecordingExecutor(),
            max_turns=max_turns,
            hook_manager=hook_manager,
        )
    ]
    return events, hist, prov


@pytest.mark.asyncio
async def test_truncated_text_continues_then_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs: list[str] = []
    events, hist, prov = await _run(
        _script(("alpha", True), ("beta", False)),
        outputs=outputs,
        monkeypatch=monkeypatch,
    )

    assert prov.stream_calls == 2
    assert _assistant_texts(hist) == ["alpha", "beta"]
    assert outputs[-1] == "alpha\nbeta"
    assert RESUME_NOTE in _message_text(prov.stream_messages[1])
    assert _interventions(events) == [("truncated_text", "continue")]
    assert _notices(events, "inlineMessage") == []
    assert _notices(events, "thinkingMessage")[0].msg == "The response was cut off. Continuing."
    assert events[-1].kind == "TurnComplete"


@pytest.mark.asyncio
async def test_three_truncations_stop_after_two_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs: list[str] = []
    events, hist, prov = await _run(
        _script(("alpha", True), ("beta", True), ("gamma", True)),
        outputs=outputs,
        monkeypatch=monkeypatch,
    )

    assert prov.stream_calls == 3
    assert _assistant_texts(hist) == ["alpha", "beta", "gamma"]
    assert outputs[-1] == "alpha\nbeta\ngamma"
    assert _interventions(events) == [
        ("truncated_text", "continue"),
        ("truncated_text", "continue"),
        ("truncated_text", "exhausted"),
    ]
    notices = _notices(events, "inlineMessage")
    assert len(notices) == 1
    assert notices[0].msg == TRUNCATED_ANSWER_NOTICE
    assert notices[0].data == {"kind": "responseTruncated"}
    assert TRUNCATED_ANSWER_NOTICE not in "\n".join(_assistant_texts(hist))
    assert events[-1].kind == "TurnComplete"


@pytest.mark.asyncio
async def test_final_answer_gate_sees_the_joined_answer() -> None:
    seen: list[str | None] = []

    async def gate(payload: HookPayload) -> None:
        seen.append(payload.assistant_text)

    manager = HookManager()
    manager.register(HookEvent.PRE_FINAL_ANSWER, gate)
    _events, _hist, _prov = await _run(
        _script(("alpha", True), ("beta", False)),
        hook_manager=manager,
    )

    assert seen == ["alpha\nbeta"]


@pytest.mark.asyncio
async def test_truncation_at_the_turn_limit_does_not_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs: list[str] = []
    events, hist, prov = await _run(
        _script(("alpha", True), ("beta", False)),
        max_turns=1,
        outputs=outputs,
        monkeypatch=monkeypatch,
    )

    assert prov.stream_calls == 1
    assert _assistant_texts(hist) == ["alpha"]
    assert outputs[-1] == "alpha"
    assert _interventions(events) == [("truncated_text", "exhausted")]
    assert _notices(events, "inlineMessage")[0].msg == TRUNCATED_ANSWER_NOTICE
    assert events[-1].kind == "TurnComplete"
