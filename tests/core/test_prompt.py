"""Tests for :mod:`monkeybot.core.prompts.prompt`."""

import pytest

from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.prompt import compose_system_prompt
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef


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
            SkillRef(name="s1", description="d1"),
            SkillRef(name="s2", description="d2"),
        ],
    )
    out = compose_system_prompt(
        ctx,
        curated_memory_skills=True,
        curated_memory_index=["a"],
        curated_skills=[SkillRef(name="s2", description="d2")],
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
        skills=[SkillRef(name="s1", description="d1")],
    )
    out = compose_system_prompt(ctx)
    assert "## Memory index" in out
    assert "- Note A" in out
    assert "## Skills" in out
    assert "- s1: d1" in out


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


def test_compose_harness_reflects_sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx)
    assert "gateway host" in out

    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    out_s = compose_system_prompt(ctx)
    assert "OpenSandbox" in out_s


def test_current_request_appears_after_harness() -> None:
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
    assert out.index("## Current request") > out.index("MonkeyBot harness")


def test_stable_prefix_byte_identical_across_turns() -> None:
    ctx = _minimal_ctx()

    def _msgs_with_task(task_text: str) -> list[Message]:
        return [
            Message(role="user", content=[Text(text=task_text)]),
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
            Message(role="assistant", content=[Text(text="Processing")]),
        ]

    out_a = compose_system_prompt(ctx, chat_messages=_msgs_with_task("Follow-up A"))
    out_b = compose_system_prompt(ctx, chat_messages=_msgs_with_task("Follow-up B"))
    assert "## Current request" in out_a
    assert "## Current request" in out_b
    prefix_a = out_a[: out_a.index("## Current request")]
    prefix_b = out_b[: out_b.index("## Current request")]
    assert "MonkeyBot harness" in prefix_a
    assert prefix_a == prefix_b


def test_memory_skills_precede_harness() -> None:
    ctx = _minimal_ctx(
        memory_index=["Note A"],
        skills=[SkillRef(name="s1", description="d1")],
    )
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
    mem_idx = out.index("## Memory index")
    skills_idx = out.index("## Skills")
    harness_idx = out.index("MonkeyBot harness")
    current_idx = out.index("## Current request")
    assert mem_idx < skills_idx < harness_idx < current_idx


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
