"""Tests for the runtime harness prompt fragment."""

import pytest

from monkeybot.core.prompts.harness_prompt import (
    HARNESS_TOOL_CALL_PROTOCOL,
    emission_style_terse_from_env,
    harness_fixed_context,
)


def test_harness_includes_core_tools_and_protocol() -> None:
    out = harness_fixed_context(include_task_tool=False)
    assert "## MonkeyBot harness (fixed)" in out
    assert "`read_file`" in out
    assert "`run_command`" in out
    assert "`task` —" not in out
    assert "### Built-in tool errors (recovery)" in out
    assert "error_kind" in out
    assert "### Tool-call protocol (strict)" in out
    assert "native function-call channel" in out
    assert '{"tool_calls":' not in out


def test_harness_includes_runtime_error_and_no_repeat_guidance() -> None:
    out = harness_fixed_context(include_task_tool=False)
    assert "runtime" in out
    assert "No-repeat rule" in out
    assert "same name and same arguments that already failed" in out
    assert "ok: false" in out


def test_harness_adds_task_line_when_enabled() -> None:
    out = harness_fixed_context(include_task_tool=True)
    assert "`task` — subprocess subagent" in out
    assert "subagent_type" in out
    assert "Nested `task` is disabled inside a subagent." in out


def test_harness_lists_subagent_personas() -> None:
    out = harness_fixed_context(
        include_task_tool=True,
        subagent_personas=(("researcher", "Deep-dives a topic."), ("analyst", "Read-only review.")),
    )
    assert "### Subagent personas" in out
    assert "`researcher` — Deep-dives a topic." in out
    assert "`analyst` — Read-only review." in out


def test_harness_protocol_is_appended_verbatim() -> None:
    out = harness_fixed_context(include_task_tool=False)
    assert out.endswith(HARNESS_TOOL_CALL_PROTOCOL)


def test_harness_injects_runtime_paths() -> None:
    out = harness_fixed_context(
        include_task_tool=False,
        workspace_root="/srv/bot",
        memory_storage_uri="local:///srv/bot/data/memory",
    )
    assert "`/srv/bot`" in out
    assert "local:///srv/bot/data/memory" in out


def test_harness_default_paths_shown_when_not_provided() -> None:
    out = harness_fixed_context(include_task_tool=False)
    assert "(not set)" in out


def test_harness_run_command_host_execution_by_default() -> None:
    out = harness_fixed_context(include_task_tool=False, run_command_opensandbox=False)
    assert "gateway host" in out
    assert "no OpenSandbox" in out


def test_harness_run_command_opensandbox_execution_when_enabled() -> None:
    out = harness_fixed_context(include_task_tool=False, run_command_opensandbox=True)
    assert "OpenSandbox" in out
    assert "bind-mounted" in out


def test_harness_omits_emission_block_by_default() -> None:
    out = harness_fixed_context(include_task_tool=True)
    assert "### Emission style (terse)" not in out
    assert "### Subagent handoffs (dense)" not in out


def test_harness_emission_style_block_when_enabled_without_task() -> None:
    out = harness_fixed_context(include_task_tool=False, emission_style=True)
    assert "### Emission style (terse)" in out
    assert "Volume is cost" in out
    # Lever 3 agent-to-agent block is gated on the task tool.
    assert "### Subagent handoffs (dense)" not in out


def test_harness_emission_includes_agent_to_agent_block_only_with_task() -> None:
    out = harness_fixed_context(include_task_tool=True, emission_style=True)
    assert "### Emission style (terse)" in out
    assert "### Subagent handoffs (dense)" in out
    assert "Minified JSON" in out


def test_harness_emission_block_precedes_protocol_and_preserves_end() -> None:
    out = harness_fixed_context(include_task_tool=True, emission_style=True)
    assert out.endswith(HARNESS_TOOL_CALL_PROTOCOL)
    assert out.index("### Emission style (terse)") < out.index("### Tool-call protocol (strict)")


def test_harness_emission_keeps_safety_carve_outs() -> None:
    out = harness_fixed_context(include_task_tool=True, emission_style=True)
    assert "evidence rule" in out
    assert "keep function bodies" in out


def test_emission_style_terse_from_env_recognizes_opt_in_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("terse", "true", "1", "on", "yes", "TERSE", " On "):
        monkeypatch.setenv("MONKEYBOT_EMISSION_STYLE", value)
        assert emission_style_terse_from_env() is True, value

    for value in ("", "off", "no", "false", "verbose"):
        monkeypatch.setenv("MONKEYBOT_EMISSION_STYLE", value)
        assert emission_style_terse_from_env() is False, value

    monkeypatch.delenv("MONKEYBOT_EMISSION_STYLE", raising=False)
    assert emission_style_terse_from_env() is False
