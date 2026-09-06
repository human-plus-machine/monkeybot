"""Tests for ToolDef model-facing schema serialization."""

from __future__ import annotations

from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._openai_compat import openai_tools
from monkeybot.providers._utils import anthropic_tool_defs


def test_to_model_schema_omits_harness_flags() -> None:
    tool = ToolDef(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        parallel_safe=True,
        doom_loop_exempt=True,
    )
    schema = tool.to_model_schema()
    assert schema == {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    assert "parallel_safe" not in schema
    assert "doom_loop_exempt" not in schema
    assert "read_only" not in schema


def test_provider_converters_omit_harness_flags() -> None:
    tools = [
        ToolDef(
            "glob",
            "List paths",
            {"type": "object"},
            parallel_safe=True,
            doom_loop_exempt=False,
        )
    ]
    anthropic = anthropic_tool_defs(tools)
    assert anthropic == [
        {"name": "glob", "description": "List paths", "input_schema": {"type": "object"}}
    ]
    assert "parallel_safe" not in anthropic[0]
    assert "doom_loop_exempt" not in anthropic[0]

    oai = openai_tools(tools)
    assert oai[0]["function"]["name"] == "glob"
    assert "parallel_safe" not in oai[0]
    assert "parallel_safe" not in oai[0]["function"]
    assert "doom_loop_exempt" not in oai[0]["function"]
