"""Tests for the runtime harness prompt fragment."""

from monkeybot.core.harness_prompt import HARNESS_TOOL_CALL_PROTOCOL, harness_fixed_context


def test_harness_includes_core_tools_and_protocol() -> None:
    out = harness_fixed_context(include_task_tool=False)
    assert "## MonkeyBot harness (fixed)" in out
    assert "`read_file`" in out
    assert "`run_command`" in out
    assert "`task` —" not in out
    assert "### Tool-call protocol (strict)" in out
    assert '{"tool_calls":' in out


def test_harness_adds_task_line_when_enabled() -> None:
    out = harness_fixed_context(include_task_tool=True)
    assert "`task` — subprocess subagent" in out
    assert "Nested `task` is disabled inside a subagent." in out


def test_harness_protocol_is_appended_verbatim() -> None:
    out = harness_fixed_context(include_task_tool=False)
    assert out.endswith(HARNESS_TOOL_CALL_PROTOCOL)
