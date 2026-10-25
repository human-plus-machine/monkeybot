"""§5.2 scenario schema: legal keys per mode, ids, events, and step models."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta, ToolCall, ToolInputDelta
from monkeybot.core.llm.provider import GroundingEvent as ProviderGrounding
from monkeybot.core.llm.provider import UsageEvent as ProviderUsage
from regression.schema.events import RaiseEvent, parse_event, to_provider_event
from regression.schema.scenario import Scenario, load_scenario, scenario_id_from_path
from regression.schema.steps import (
    AdvanceClockStep,
    AnswerAskUserStep,
    CancelStep,
    ClientTextStep,
    ConfirmToolStep,
    DisconnectSseStep,
    InterruptStep,
    PromoteFollowUpStep,
    QueueFollowUpStep,
    ReconnectSseStep,
    ReloadConfigStep,
    SteerStep,
    TouchFileStep,
)

_MODES = ["scripted", "replay", "live"]


def _load(tmp_path: Path, body: str, name: str = "case.yaml") -> Scenario:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(body).strip() + "\n", encoding="utf-8")
    return load_scenario(path, root=tmp_path)


def _reject(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _load(tmp_path, body)


def _scenario(mode: str, extra: str = "") -> str:
    body = dedent(extra).strip()
    prefix = f"case: T2-LOOP-07\nmode: {mode}\n"
    if not body:
        return prefix
    return prefix + body + "\n"


def test_unknown_key(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "nope: 1\n"), "nope")


def test_key_used_in_the_wrong_mode(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("scripted", "cassette: t3/trajectories/x.json\n"),
        "cassette",
    )


def test_missing_case(tmp_path: Path) -> None:
    _reject(tmp_path, "mode: scripted\n", "case")


def test_blank_case(tmp_path: Path) -> None:
    _reject(tmp_path, "case: '   '\nmode: scripted\n", "case")


def test_missing_mode(tmp_path: Path) -> None:
    _reject(tmp_path, "case: T2-LOOP-07\n", "mode")


def test_unknown_mode(tmp_path: Path) -> None:
    _reject(tmp_path, "case: T2-LOOP-07\nmode: t2\n", "mode")


def test_id_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "case.yaml"
    path.write_text("case: T2-LOOP-07\nmode: scripted\nid: other\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_scenario(path, root=tmp_path)


def test_id_matches_when_present(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        "id: t2/loop/scenarios/loop/doom\ncase: T2-LOOP-07\nmode: scripted\n",
        "t2/loop/scenarios/loop/doom.yaml",
    )
    assert scenario.id == "t2/loop/scenarios/loop/doom"


def test_id_is_derived_when_omitted(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        _scenario("live"),
        "t4/scenarios/coding/fix_failing_test.yml",
    )
    assert scenario.id == "t4/scenarios/coding/fix_failing_test"


def test_id_from_repo_root_uses_the_repo_relative_path() -> None:
    repo = Path(__file__).resolve().parents[2]
    path = repo / "regression" / "t2" / "loop" / "scenarios" / "loop" / "doom.yaml"
    assert scenario_id_from_path(path) == "regression/t2/loop/scenarios/loop/doom"


def test_invariants_skip_without_a_reason(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "invariants_skip:\n  - name: strict_script\n",
        ),
        "reason",
    )


def test_invariants_skip_blank_reason(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "invariants_skip:\n  - {name: strict_script, reason: '   '}\n",
        ),
        "reason",
    )


def test_invariants_skip_string_item_is_not_enough(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("replay", "invariants_skip: [strict_script]\n"),
        "InvariantSkip",
    )


def test_invariants_skip_with_reason(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            "replay",
            "invariants_skip:\n  - {name: strict_script, reason: partial cassette}\n",
        ),
    )
    assert scenario.invariants_skip[0].name == "strict_script"
    assert scenario.invariants_skip[0].reason == "partial cassette"


def test_invariants_skip_unknown_field(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "invariants_skip:\n  - {name: strict_script, reason: because, note: extra}\n",
        ),
        "note",
    )


@pytest.mark.parametrize("mode", _MODES)
def test_common_metadata(tmp_path: Path, mode: str) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            mode,
            "description: A case\ntags: [alpha, beta]\nregression_of: https://example.test/1\n",
        ),
    )
    assert scenario.description == "A case"
    assert scenario.tags == ["alpha", "beta"]
    assert scenario.regression_of == "https://example.test/1"


@pytest.mark.parametrize("mode", _MODES)
def test_messages_session_pause_and_workspace(tmp_path: Path, mode: str) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            mode,
            """
            messages:
              - Say hello in {workspace_subdir}
            session_pause_sec: 1.5
            workspace_fixture: calc_repo
            workspace_overlays:
              - overlays/one
            """,
        ),
    )
    assert scenario.messages == ["Say hello in {workspace_subdir}"]
    assert scenario.session_pause_sec == 1.5
    assert scenario.workspace_fixture == "calc_repo"
    assert scenario.workspace_overlays == ["overlays/one"]
    assert scenario.sessions is None


@pytest.mark.parametrize("mode", _MODES)
def test_sessions(tmp_path: Path, mode: str) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            mode,
            """
            sessions:
              - - Remember project-marigold
              - - What was the codename?
            session_pause_sec: 8
            """,
        ),
    )
    assert scenario.sessions == [["Remember project-marigold"], ["What was the codename?"]]
    assert scenario.session_pause_sec == 8
    assert scenario.messages == []


@pytest.mark.parametrize("mode", _MODES)
def test_messages_and_sessions_together_fail(tmp_path: Path, mode: str) -> None:
    _reject(
        tmp_path,
        _scenario(mode, "messages: [hi]\nsessions:\n  - [there]\n"),
        "messages and sessions",
    )


def test_negative_session_pause_fails(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("live", "session_pause_sec: -1\n"), "session_pause_sec")


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "assertions",
    ["assertions:\n  turn_completed: true\n", "assertions:\n  - turn_completed\n"],
)
def test_assertions_accepted_unparsed(tmp_path: Path, mode: str, assertions: str) -> None:
    scenario = _load(tmp_path, _scenario(mode, assertions))
    assert scenario.assertions in ({"turn_completed": True}, ["turn_completed"])


@pytest.mark.parametrize("mode", _MODES)
def test_config_overrides_and_env(tmp_path: Path, mode: str) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            mode,
            """
            config:
              model.context_window: 4000
              env:
                MODEL_X: "on"
            """,
        ),
    )
    assert scenario.config is not None
    dumped = scenario.config.model_dump()
    assert dumped["model.context_window"] == 4000
    assert dumped["env"] == {"MODEL_X": "on"}


def test_config_env_must_be_strings(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("live", "config:\n  env:\n    MODEL_X: 1\n"),
        "env",
    )


@pytest.mark.parametrize("mode", ["scripted", "replay"])
@pytest.mark.parametrize("driver", ["loop", "gateway", "realtime"])
def test_driver_values(tmp_path: Path, mode: str, driver: str) -> None:
    scenario = _load(tmp_path, _scenario(mode, f"driver: {driver}\n"))
    assert scenario.driver == driver


@pytest.mark.parametrize("driver", ["loop", "gateway", "realtime"])
def test_driver_illegal_for_live(tmp_path: Path, driver: str) -> None:
    _reject(tmp_path, _scenario("live", f"driver: {driver}\n"), "driver")


def test_unknown_driver(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "driver: sse\n"), "driver")


@pytest.mark.parametrize("mode", ["scripted", "replay"])
@pytest.mark.parametrize("executor", ["recording", "real"])
def test_executor_values(tmp_path: Path, mode: str, executor: str) -> None:
    scenario = _load(tmp_path, _scenario(mode, f"executor: {executor}\n"))
    assert scenario.executor == executor


def test_executor_illegal_for_live(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("live", "executor: recording\n"), "executor")


def test_unknown_executor(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "executor: fake\n"), "executor")


def test_scripted_defaults(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("scripted"))
    assert scenario.driver == "loop"
    assert scenario.executor == "recording"
    assert scenario.script_partial is False
    assert scenario.script is None
    assert scenario.tool_results is None
    assert scenario.cassette is None
    assert scenario.trials is None


def test_replay_defaults(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("replay"))
    assert scenario.driver == "loop"
    assert scenario.executor == "recording"
    assert scenario.script_partial is None
    assert scenario.cassette is None


def test_live_defaults(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("live"))
    assert scenario.driver is None
    assert scenario.executor is None
    assert scenario.script_partial is None
    assert scenario.trials is None


def test_script_partial_true(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("scripted", "script_partial: true\n"))
    assert scenario.script_partial is True


@pytest.mark.parametrize("mode", ["replay", "live"])
def test_script_partial_illegal_outside_scripted(tmp_path: Path, mode: str) -> None:
    _reject(tmp_path, _scenario(mode, "script_partial: false\n"), "script_partial")


def test_script_accepted_for_scripted(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("scripted", "script: []\n"))
    assert scenario.script == []


@pytest.mark.parametrize("mode", ["replay", "live"])
def test_script_illegal_outside_scripted(tmp_path: Path, mode: str) -> None:
    _reject(tmp_path, _scenario(mode, "script: []\n"), "script")


def test_cassette_accepted_for_replay(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("replay", "cassette: t3/trajectories/ollama/fix.json\n"))
    assert scenario.cassette == "t3/trajectories/ollama/fix.json"


@pytest.mark.parametrize("mode", ["scripted", "live"])
def test_cassette_illegal_outside_replay(tmp_path: Path, mode: str) -> None:
    _reject(tmp_path, _scenario(mode, "cassette: t3/trajectories/x.json\n"), "cassette")


def test_empty_cassette_fails(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("replay", "cassette: '   '\n"), "cassette")


def test_tool_results_with_default_recording_executor(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        _scenario("scripted", "tool_results:\n  c1:\n    text: ok\n"),
    )
    assert scenario.tool_results == {"c1": {"text": "ok"}}
    assert scenario.executor == "recording"


def test_tool_results_with_explicit_recording_executor(tmp_path: Path) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            "scripted",
            "executor: recording\ntool_results:\n  c1:\n    text: ok\n",
        ),
    )
    assert scenario.tool_results == {"c1": {"text": "ok"}}


def test_tool_results_illegal_when_executor_is_real(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "executor: real\ntool_results:\n  c1:\n    text: ok\n",
        ),
        "tool_results",
    )


@pytest.mark.parametrize("mode", ["replay", "live"])
def test_tool_results_illegal_outside_scripted(tmp_path: Path, mode: str) -> None:
    _reject(
        tmp_path,
        _scenario(mode, "tool_results:\n  c1:\n    text: ok\n"),
        "tool_results",
    )


def test_tool_results_envelope_must_be_a_mapping(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "tool_results:\n  c1: ok\n"), "tool_results")


def test_trials_accepted_for_live(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("live", "trials: 8\n"))
    assert scenario.trials == 8


@pytest.mark.parametrize("mode", ["scripted", "replay"])
def test_trials_illegal_outside_live(tmp_path: Path, mode: str) -> None:
    _reject(tmp_path, _scenario(mode, "trials: 8\n"), "trials")


def test_trials_must_be_positive(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("live", "trials: 0\n"), "trials")


@pytest.mark.parametrize("mode", ["scripted", "replay"])
def test_hooks_policy_memory_and_history(tmp_path: Path, mode: str) -> None:
    scenario = _load(
        tmp_path,
        _scenario(
            mode,
            """
            hooks:
              - verification_gate
              - evidence_guard
              - secret_scanner
              - verifier
              - git_context
              - read_ledger_compaction
              - memory
            policy:
              command_allowlist: strict
              permissions: overlap
              grants: default
            memory:
              index:
                - project-alpha is the main initiative for Q3
              palace:
                - drawer_id: d1
                  content: remembered
            history:
              - role: user
                content:
                  - type: text
                    text: earlier
            """,
        ),
    )
    assert scenario.hooks == [
        "verification_gate",
        "evidence_guard",
        "secret_scanner",
        "verifier",
        "git_context",
        "read_ledger_compaction",
        "memory",
    ]
    assert scenario.policy is not None
    assert scenario.policy.command_allowlist == "strict"
    assert scenario.policy.permissions == "overlap"
    assert scenario.policy.grants == "default"
    assert scenario.memory is not None
    assert scenario.memory.index == ["project-alpha is the main initiative for Q3"]
    assert scenario.memory.palace[0].drawer_id == "d1"
    assert scenario.memory.palace[0].wing == "main"
    assert scenario.memory.palace[0].room == "conversation"
    assert scenario.history[0].to_dict() == {
        "role": "user",
        "content": [{"type": "text", "text": "earlier"}],
    }


@pytest.mark.parametrize("key", ["hooks", "policy", "memory", "history", "steps"])
def test_shared_scripted_replay_keys_illegal_for_live(tmp_path: Path, key: str) -> None:
    snippets = {
        "hooks": "hooks: [memory]\n",
        "policy": "policy:\n  grants: default\n",
        "memory": "memory:\n  index: []\n",
        "history": "history: []\n",
        "steps": "steps: []\n",
    }
    _reject(tmp_path, _scenario("live", snippets[key]), key)


def test_unknown_hook_bundle(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "hooks: [not_a_bundle]\n"), "not_a_bundle")


def test_unknown_policy_key(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "policy:\n  sandbox: default\n"), "sandbox")


def test_unknown_memory_key(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("replay", "memory:\n  files: []\n"), "files")


def test_history_must_be_messages(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            """
            history:
              - role: narrator
                content:
                  - type: text
                    text: no
            """,
        ),
        "history",
    )


_STEPS = """
steps:
  - action: cancel
    when: {event: text, nth: 1}
  - action: steer
    text: do this instead
    when: {event: tool_result, nth: 1, name: read_file}
  - action: queue_follow_up
    text: also check the tests
    request_id: fu1
    when: {event: done}
  - action: promote_follow_up
    request_id: fu1
    when: {event: done, nth: 2}
  - action: answer_ask_user
    answer: "yes"
    call_id: ask1
    when: {event: ask_user}
  - action: confirm_tool
    decision: approve
    call_id: c1
    when: {event: tool_confirmation}
  - action: confirm_tool
    decision: deny
    reason: not this file
    when: {event: tool_confirmation, nth: 2}
  - action: confirm_tool
    decision: always
    when: {event: tool_confirmation, nth: 3}
  - action: touch_file
    path: calc/ops.py
    when: {event: tool_result, call_id: c1}
  - action: touch_file
    path: calc/ops.py
    mtime: 1000
    size: 12
    when: {event: tool_result, nth: 2}
  - action: advance_clock
    ms: 5000
    when: {event: text, nth: 2}
  - action: reload_config
    config: {model.context_window: 2000}
    when: {event: done, nth: 3}
  - action: disconnect_sse
    when: {event: sse_open}
  - action: reconnect_sse
    last_event_id: "17"
    when: {event: sse_open, nth: 2}
  - action: client_text
    text: hello from the client
    when: {event: turn_boundary}
  - action: interrupt
    when: {event: text, nth: 3}
"""


@pytest.mark.parametrize("mode", ["scripted", "replay"])
def test_steps_cover_every_action(tmp_path: Path, mode: str) -> None:
    scenario = _load(tmp_path, _scenario(mode, _STEPS))
    assert [type(step) for step in scenario.steps] == [
        CancelStep,
        SteerStep,
        QueueFollowUpStep,
        PromoteFollowUpStep,
        AnswerAskUserStep,
        ConfirmToolStep,
        ConfirmToolStep,
        ConfirmToolStep,
        TouchFileStep,
        TouchFileStep,
        AdvanceClockStep,
        ReloadConfigStep,
        DisconnectSseStep,
        ReconnectSseStep,
        ClientTextStep,
        InterruptStep,
    ]
    cancel = scenario.steps[0]
    assert isinstance(cancel, CancelStep)
    assert cancel.when.event == "text"
    assert cancel.when.nth == 1
    steer = scenario.steps[1]
    assert isinstance(steer, SteerStep)
    assert steer.text == "do this instead"
    assert steer.when.name == "read_file"
    follow = scenario.steps[2]
    assert isinstance(follow, QueueFollowUpStep)
    assert follow.request_id == "fu1"
    assert follow.when.nth == 1
    decisions = [step.decision for step in scenario.steps if isinstance(step, ConfirmToolStep)]
    assert decisions == ["approve", "deny", "always"]
    touched = [step for step in scenario.steps if isinstance(step, TouchFileStep)]
    assert touched[0].mtime is None and touched[0].size is None
    assert touched[1].mtime == 1000
    assert touched[1].size == 12
    assert touched[0].when.call_id == "c1"
    clock = next(step for step in scenario.steps if isinstance(step, AdvanceClockStep))
    assert clock.ms == 5000
    reload = next(step for step in scenario.steps if isinstance(step, ReloadConfigStep))
    assert reload.config == {"model.context_window": 2000}
    reconnect = next(step for step in scenario.steps if isinstance(step, ReconnectSseStep))
    assert reconnect.last_event_id == "17"
    client = next(step for step in scenario.steps if isinstance(step, ClientTextStep))
    assert client.text == "hello from the client"
    assert any(isinstance(step, InterruptStep) for step in scenario.steps)
    assert any(isinstance(step, DisconnectSseStep) for step in scenario.steps)


def test_step_requires_a_predicate(tmp_path: Path) -> None:
    _reject(tmp_path, _scenario("scripted", "steps:\n  - action: cancel\n"), "when")


def test_unknown_step_action(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("scripted", "steps:\n  - action: explode\n    when: {event: text}\n"),
        "action",
    )


def test_unknown_step_field(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "steps:\n  - action: cancel\n    when: {event: text}\n    extra: 1\n",
        ),
        "extra",
    )


def test_confirm_tool_decision_must_be_known(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "steps:\n  - action: confirm_tool\n    decision: maybe\n    when: {event: tool_confirmation}\n",
        ),
        "decision",
    )


def test_reconnect_sse_requires_last_event_id(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "replay",
            "steps:\n  - action: reconnect_sse\n    when: {event: sse_open}\n",
        ),
        "last_event_id",
    )


def test_touch_file_rejects_negative_size(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            """
            steps:
              - action: touch_file
                path: calc/ops.py
                size: -1
                when: {event: tool_result}
            """,
        ),
        "size",
    )


def test_predicate_nth_starts_at_one(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario(
            "scripted",
            "steps:\n  - action: cancel\n    when: {event: text, nth: 0}\n",
        ),
        "nth",
    )


_SCRIPT = """
script:
  - - type: thinking
      text: hmm
      signature: sig
    - type: text
      text: hello
    - type: tool_input_delta
      call_id: c1
      name: read_file
      delta: '{"path":'
    - type: tool_call
      call_id: c1
      name: read_file
      args:
        path: a.py
      parse_error: bad json
      metadata:
        source: model
    - type: grounding
      sources:
        - title: Doc
          uri: https://example.test
      search_queries: [calc]
    - type: usage
      input_tokens: 3
      output_tokens: 4
      cached_tokens: 1
    - type: done
      truncated: true
  - - type: text
      text: before raise
    - type: raise
      error: TimeoutError
      after: 1
      message: stopped
    - type: done
"""


def test_script_events_map_onto_provider_events(tmp_path: Path) -> None:
    scenario = _load(tmp_path, _scenario("scripted", _SCRIPT))
    assert scenario.script is not None
    first = [to_provider_event(event) for event in scenario.script[0]]
    assert first == [
        ThinkingDelta(text="hmm", signature="sig"),
        TextDelta(text="hello"),
        ToolInputDelta(call_id="c1", name="read_file", delta='{"path":'),
        ToolCall(
            call_id="c1",
            name="read_file",
            args={"path": "a.py"},
            parse_error="bad json",
            metadata={"source": "model"},
        ),
        ProviderGrounding(
            sources=[{"title": "Doc", "uri": "https://example.test"}],
            search_queries=["calc"],
        ),
        ProviderUsage(
            input_tokens=3,
            output_tokens=4,
            cached_tokens=1,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        ),
        Done(truncated=True),
    ]
    second = scenario.script[1]
    assert to_provider_event(second[0]) == TextDelta(text="before raise")
    assert isinstance(second[1], RaiseEvent)
    assert second[1].error == "TimeoutError"
    assert second[1].after == 1
    assert second[1].message == "stopped"
    with pytest.raises(TypeError, match="raise is not a ProviderEvent"):
        to_provider_event(second[1])
    assert to_provider_event(second[2]) == Done(truncated=False)


def test_done_truncated_defaults_false() -> None:
    event = parse_event({"type": "done"})
    assert to_provider_event(event) == Done(truncated=False)


def test_thinking_signature_defaults_none() -> None:
    event = parse_event({"type": "thinking", "text": "hmm"})
    assert to_provider_event(event) == ThinkingDelta(text="hmm", signature=None)


def test_tool_call_args_default_empty() -> None:
    event = parse_event({"type": "tool_call", "call_id": "c1", "name": "read_file"})
    assert to_provider_event(event) == ToolCall(call_id="c1", name="read_file", args={})


def test_unknown_event_type(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("scripted", "script:\n  - - type: text_delta\n      text: hi\n"),
        "type",
    )


def test_unknown_event_field(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("scripted", "script:\n  - - type: text\n      text: hi\n      nope: 1\n"),
        "nope",
    )


def test_raise_requires_an_error_class(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _scenario("scripted", "script:\n  - - type: raise\n"),
        "error",
    )
