"""Tests for :mod:`monkeybot.core.prompt`."""

import pytest

from monkeybot.core.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.prompt import compose_system_prompt
from monkeybot.core.provider import Message
from monkeybot.core.types_tools import ToolDef


def _minimal_ctx(
    *,
    agent_md: str = "You are TestBot.",
    memory_index: list[str] | None = None,
    skills: list[SkillRef] | None = None,
    tools: list[ToolDef] | None = None,
) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md=agent_md,
        memory_index=memory_index or [],
        skills=skills or [],
        tools=tools or [],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )


def test_compose_curated_omits_unlisted_memory_and_skills() -> None:
    ctx = _minimal_ctx(
        memory_index=["a", "b"],
        skills=[
            SkillRef(name="s1", description="d1", entry_point="s1/run.py"),
            SkillRef(name="s2", description="d2", entry_point="s2/run.py"),
        ],
    )
    out = compose_system_prompt(
        ctx,
        curated_memory_skills=True,
        curated_memory_index=["a"],
        curated_skills=[SkillRef(name="s2", description="d2", entry_point="s2/run.py")],
    )
    assert "- a" in out
    assert "- b" not in out
    assert "s2" in out
    assert "s1" not in out


def test_compose_no_chat_skips_current_request() -> None:
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx, chat_messages=None)
    assert "You are TestBot." in out
    assert "## Current request" not in out
    assert "MonkeyBot harness" in out


def test_compose_last_message_user_skips_duplicate_task() -> None:
    ctx = _minimal_ctx()
    msgs = [Message(role="user", content=[Text(text="Hello")])]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "## Current request" not in out


def test_compose_injects_current_request_after_tool_round() -> None:
    ctx = _minimal_ctx()
    msgs = [
        Message(role="user", content=[Text(text="Do the thing")]),
        Message(
            role="assistant",
            content=[ToolRequest(id="c1", name="read_file", args={})],
        ),
        Message(
            role="user",
            content=[
                ToolResponse(
                    id="c1",
                    tool_name="read_file",
                    result=[Text(text="ok")],
                    is_error=False,
                )
            ],
        ),
    ]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "## Current request" in out
    assert "Do the thing" in out


def test_memory_and_skills_sections() -> None:
    ctx = _minimal_ctx(
        memory_index=["Note A"],
        skills=[SkillRef(name="s1", description="d1", entry_point="s1/run.py")],
    )
    out = compose_system_prompt(ctx)
    assert "## Memory index" in out
    assert "- Note A" in out
    assert "## Skills" in out
    assert "s1" in out


def test_task_truncation() -> None:
    ctx = _minimal_ctx()
    long_user = "x" * 9000
    msgs = [
        Message(role="user", content=[Text(text=long_user)]),
        Message(role="assistant", content=[Text(text="calling tool")]),
    ]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "…(truncated)" in out
    assert len(out) < len(long_user) + 5000


@pytest.mark.parametrize("include_task", [True, False])
def test_harness_task_line_matches_tools(include_task: bool) -> None:
    tools: list[ToolDef] = []
    if include_task:
        tools.append(
            ToolDef(
                "task",
                "subagent",
                {"type": "object", "properties": {}, "required": []},
            )
        )
    ctx = _minimal_ctx(tools=tools)
    out = compose_system_prompt(ctx)
    if include_task:
        assert "- `task` — subprocess" in out
    else:
        assert "- `task` — subprocess" not in out
