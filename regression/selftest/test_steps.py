"""One tiny scripted scenario per loop-driver step.

Gateway and realtime steps are rejected here. Their drivers own them.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from monkeybot.core.runtime.events import ContextUsage, UserSteered
from monkeybot.core.types.content_blocks import Text
from regression.drivers.loop import LoopDriverError, run_scenario
from regression.schema.assertions import event_field, event_type
from regression.schema.scenario import Scenario, load_scenario
from regression.schema.steps import StepFired

_TIMEOUT_S = 20


def _load(tmp_path: Path, name: str, body: str) -> Scenario:
    path = tmp_path / f"{name}.yaml"
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return load_scenario(path, root=tmp_path)


async def _run(scenario: Scenario):
    return await asyncio.wait_for(run_scenario(scenario), timeout=_TIMEOUT_S)


def _kinds(events: list[object]) -> list[str]:
    return [event_type(event) or type(event).__name__ for event in events]


def _actions(events: list[object]) -> list[str]:
    return [event.action for event in events if isinstance(event, StepFired)]


@pytest.mark.asyncio
async def test_cancel_sets_the_run_event(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "cancel",
        """
        case: P1-03-cancel
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: done
        steps:
          - action: cancel
            when: {event: text}
        """,
    )
    result = await _run(scenario)
    assert "cancel" in _actions(result.events), _kinds(result.events)
    assert any(
        event_type(event) == "Error" and event_field(event, "error") == "Request cancelled"
        for event in result.events
    ), _kinds(result.events)


@pytest.mark.asyncio
async def test_steer_is_drained_on_the_next_inner_turn(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "steer",
        """
        case: P1-03-steer
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: tool_call
              call_id: c1
              name: read_file
              args:
                path: a.py
            - type: done
          - - type: text
              text: after
            - type: done
        steps:
          - action: steer
            text: do this instead
            when: {event: text}
        """,
    )
    result = await _run(scenario)
    steered = [event for event in result.events if isinstance(event, UserSteered)]
    assert [event.text for event in steered] == ["do this instead"], _kinds(result.events)
    assert "steer" in _actions(result.events)


@pytest.mark.asyncio
async def test_queue_follow_up_is_accepted(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "queue",
        """
        case: P1-03-queue
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: done
        steps:
          - action: queue_follow_up
            text: also check the tests
            request_id: fu1
            when: {event: done}
        """,
    )
    result = await _run(scenario)
    accepted = [
        event
        for event in result.events
        if event_type(event) == "QueuedInputAccepted"
        and event_field(event, "queue") == "follow_up"
        and event_field(event, "request_id") == "fu1"
    ]
    assert accepted, _kinds(result.events)
    assert "queue_follow_up" in _actions(result.events)


@pytest.mark.asyncio
async def test_promote_follow_up_becomes_a_steer(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "promote",
        """
        case: P1-03-promote
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: tool_call
              call_id: c1
              name: read_file
              args:
                path: a.py
            - type: done
          - - type: text
              text: after
            - type: done
        steps:
          - action: queue_follow_up
            text: also check the tests
            request_id: fu1
            when: {event: text}
          - action: promote_follow_up
            request_id: fu1
            when: {event: tool_result}
        """,
    )
    result = await _run(scenario)
    steered = [event for event in result.events if isinstance(event, UserSteered)]
    assert len(steered) == 1, _kinds(result.events)
    assert steered[0].queued_request_id == "fu1"
    assert steered[0].text == "also check the tests"
    assert "promote_follow_up" in _actions(result.events)


@pytest.mark.asyncio
async def test_answer_ask_user_resumes_the_tool(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "ask",
        """
        case: P1-03-ask
        mode: scripted
        executor: real
        messages: [go]
        config:
          env:
            PENDING_RESPONSE_TIMEOUT_SEC: "2"
        script:
          - - type: tool_call
              call_id: ask1
              name: ask_user
              args:
                question: Ship it?
                choices: ["yes", "no"]
            - type: done
          - - type: text
              text: thanks
            - type: done
        steps:
          - action: answer_ask_user
            answer: "yes"
            call_id: ask1
            when: {event: ask_user}
        """,
    )
    result = await _run(scenario)
    assert any(event_type(event) == "AskUserRequest" for event in result.events), _kinds(
        result.events
    )
    assert any(
        event_type(event) == "ToolCallResult" and "yes" in str(event_field(event, "result") or "")
        for event in result.events
    ), _kinds(result.events)
    assert "answer_ask_user" in _actions(result.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "detail"),
    [
        ("approve", "confirm_approved"),
        ("deny", "not this file"),
        ("always", "confirm_always"),
    ],
)
async def test_confirm_tool(tmp_path: Path, decision: str, detail: str) -> None:
    reason = "reason: not this file\n" if decision == "deny" else ""
    scenario = _load(
        tmp_path,
        decision,
        f"""
        case: P1-03-confirm-{decision}
        mode: scripted
        messages: [go]
        config:
          env:
            PENDING_RESPONSE_TIMEOUT_SEC: "2"
        script:
          - - type: tool_call
              call_id: c1
              name: read_file
              args:
                path: a.py
            - type: done
          - - type: text
              text: after
            - type: done
        steps:
          - action: confirm_tool
            decision: {decision}
            call_id: c1
            {reason}
            when: {{event: tool_confirmation}}
        """,
    )
    result = await _run(scenario)
    assert "confirm_tool" in _actions(result.events), _kinds(result.events)
    if decision == "deny":
        assert any(
            event_type(event) == "ToolCallResult" and event_field(event, "error") == detail
            for event in result.events
        ), _kinds(result.events)
        assert result.calls == []
    else:
        assert any(
            event_type(event) == "ToolCallStarted"
            and event_field(event, "inspector_decision") == detail
            for event in result.events
        ), _kinds(result.events)
        assert result.calls and result.calls[0].call_id == "c1"


@pytest.mark.asyncio
async def test_touch_file_changes_size_and_mtime(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "touch",
        """
        case: P1-03-touch
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: done
        steps:
          - action: touch_file
            path: notes.txt
            mtime: 1000
            size: 12
            when: {event: text}
        """,
    )
    result = await _run(scenario)
    fired = [event for event in result.events if isinstance(event, StepFired)]
    assert any(
        event.action == "touch_file" and "size=12" in event.detail and "mtime=1000" in event.detail
        for event in fired
    ), [event.detail for event in fired]


@pytest.mark.asyncio
async def test_advance_clock_calls_fake_clock(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "clock",
        """
        case: P1-03-clock
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: done
        steps:
          - action: advance_clock
            ms: 5000
            when: {event: text}
        """,
    )
    result = await _run(scenario)
    assert result.clock.now_ms() == 5000
    assert any(
        isinstance(event, StepFired)
        and event.action == "advance_clock"
        and event.detail == "now_ms=5000"
        for event in result.events
    ), _actions(result.events)


@pytest.mark.asyncio
async def test_reload_config_applies_on_the_next_message(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "reload",
        """
        case: P1-03-reload
        mode: scripted
        messages: [first, second]
        invariants_skip:
          - name: turn_complete
            reason: reload applies on the next user message, which completes a second turn
        script:
          - - type: text
              text: hello
            - type: done
          - - type: text
              text: next
            - type: done
        steps:
          - action: reload_config
            config:
              model.context_window: 2000
            when: {event: text, nth: 1}
        """,
    )
    result = await _run(scenario)
    assert any(event_type(event) == "ConfigReloaded" for event in result.events), _kinds(
        result.events
    )
    windows = [
        event.context_window_tokens for event in result.events if isinstance(event, ContextUsage)
    ]
    assert 200_000 in windows and 2000 in windows, windows


@pytest.mark.asyncio
async def test_rewind_drops_rows_after_the_anchor(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "rewind",
        """
        case: P1-10-rewind
        mode: scripted
        messages: [go]
        history:
          - role: user
            content:
              - type: text
                text: keep-me
          - role: assistant
            content:
              - type: text
                text: drop-me
        script:
          - - type: text
              text: after
            - type: done
        steps:
          - action: rewind
            through_text: keep-me
            when: {event: text, nth: 1}
        """,
    )
    result = await _run(scenario)
    assert any(isinstance(event, StepFired) and event.action == "rewind" for event in result.events)
    kept = "\n".join(
        block.text
        for row in result.view.history
        for block in row.content
        if isinstance(block, Text)
    )
    assert "keep-me" in kept
    assert "drop-me" not in kept


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "extra", "driver"),
    [
        ("disconnect_sse", "", "gateway"),
        ("reconnect_sse", "last_event_id: '17'", "gateway"),
        ("client_text", "text: hello from the client", "realtime"),
        ("interrupt", "", "realtime"),
    ],
)
async def test_foreign_steps_are_rejected(
    tmp_path: Path, action: str, extra: str, driver: str
) -> None:
    scenario = _load(
        tmp_path,
        action,
        f"""
        case: P1-03-{action}
        mode: scripted
        messages: [go]
        script:
          - - type: text
              text: hello
            - type: done
        steps:
          - action: {action}
            {extra}
            when: {{event: text}}
        """,
    )
    with pytest.raises(LoopDriverError, match=driver):
        await _run(scenario)
