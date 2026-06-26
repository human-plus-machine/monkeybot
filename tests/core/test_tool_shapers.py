"""Tests for content-aware tool output shaping."""

from __future__ import annotations

import json

import pytest

from monkeybot.core.context.tool_output_policy import (
    ToolOutputBudget,
    load_tool_output_policies,
    parse_tool_output_section,
    reset_tool_output_policy_cache_for_tests,
    validate_tool_output_budgets,
)
from monkeybot.core.context.tool_shapers import (
    classify_content,
    exceeds_tool_output_budget,
    shape_json,
    shape_logs,
    shape_messages_tool_results,
    shape_tool_text,
)
from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import Text, ToolResponse


def test_classify_json_and_logs() -> None:
    assert classify_content('{"a": 1}', tool_name="x") == "json"
    assert classify_content("line1\nline2\n" * 20, tool_name="run_command") == "logs"
    assert classify_content("def foo():\n  pass", tool_name="read_file") == "code"


def test_shape_json_dedups_and_caps() -> None:
    payload = json.dumps([{"id": i} for i in range(100)])
    out = shape_json(payload, max_array_items=5)
    parsed = json.loads(out)
    assert len(parsed) <= 6
    assert any(isinstance(x, str) and "more items" in x for x in parsed)


def test_shape_logs_keeps_error_lines() -> None:
    lines = ["ok"] * 50 + ["ERROR: disk full"] + ["ok"] * 50
    text = "\n".join(lines)
    out = shape_logs(
        text,
        max_lines=20,
        keep_patterns=(r"(?i)error",),
        collapse_repeated=True,
    )
    assert "ERROR: disk full" in out
    assert "repeated" in out or "omitted" in out


def test_shape_tool_text_leaves_code_intact() -> None:
    code = "def foo():\n    return 1\n"
    budget = ToolOutputBudget(content_type="code")
    assert shape_tool_text(code, tool_name="read_file", budget=budget) == code


def test_exceeds_tool_output_budget_detects_long_logs() -> None:
    text = "\n".join(f"line {i}" for i in range(500))
    budget = ToolOutputBudget(max_output_lines=400)
    assert exceeds_tool_output_budget(text, tool_name="run_command", budget=budget)


def test_shape_messages_protects_recent_tail() -> None:
    old = Message(
        role="user",
        content=[
            ToolResponse(
                id="1",
                tool_name="run_command",
                result=[Text(text="\n".join(f"line {i}" for i in range(500)))],
                is_error=False,
            )
        ],
    )
    recent = Message(
        role="user",
        content=[
            ToolResponse(
                id="2",
                tool_name="run_command",
                result=[Text(text="\n".join(f"fresh {i}" for i in range(500)))],
                is_error=False,
            )
        ],
    )
    msgs = [old, Message(role="assistant", content=[Text(text="ok")]), recent]
    shaped = shape_messages_tool_results(msgs, protect_recent=1, pressure_tier="moderate")
    old_text = shaped[0].content[0].result[0].text  # type: ignore[union-attr]
    recent_text = shaped[-1].content[0].result[0].text  # type: ignore[union-attr]
    assert len(old_text.splitlines()) < 500
    assert len(recent_text.splitlines()) == 500


def test_parse_tool_output_section(tmp_path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        "tool_output:\n  run_command:\n    max_output_lines: 100\n",
        encoding="utf-8",
    )
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    policies = parse_tool_output_section(path, data)
    assert policies["run_command"].max_output_lines == 100


def test_parse_tool_output_can_disable_builtin_collapse(tmp_path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        "tool_output:\n  run_command:\n    collapse_repeated: false\n    keep_patterns: []\n",
        encoding="utf-8",
    )
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    policies = parse_tool_output_section(path, data)
    assert policies["run_command"].collapse_repeated is False
    assert policies["run_command"].keep_patterns == ()


def test_validate_tool_output_budgets_warns_on_tiny_caps() -> None:
    warnings = validate_tool_output_budgets(
        {"x": ToolOutputBudget(max_output_lines=2, max_array_items=1)}
    )
    assert len(warnings) == 2


def test_load_tool_output_policies_from_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_tool_output_policy_cache_for_tests()
    path = tmp_path / "command_allowlist.yaml"
    path.write_text(
        "allowed_commands: [echo]\nallowed_path_prefixes: [./]\n"
        "tool_output:\n  web_search:\n    max_array_items: 10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", str(path))
    reset_tool_output_policy_cache_for_tests()
    policies = load_tool_output_policies()
    assert policies["web_search"].max_array_items == 10
