"""Tests for :mod:`monkeybot.core.prompts.prompt`."""

import pytest

from monkeybot.core.attachments.catalog import AttachmentRecord
from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.context.memory_prompt import MemoryPromptSelection
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.prompt import compose_system_prompt
from monkeybot.providers._utils import split_system_prompt_for_cache
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


def test_compose_memory_selection_omits_unlisted_lines() -> None:
    ctx = _minimal_ctx(
        memory_index=["a", "b"],
        skills=[
            SkillRef(name="s1", description="d1"),
            SkillRef(name="s2", description="d2"),
        ],
    )
    selection = MemoryPromptSelection(
        lines=["a"],
        total_lines=2,
        coverage=0.5,
        confidence=0.5,
        nudge_search=True,
        use_custom_lines=True,
    )
    out = compose_system_prompt(ctx, memory_selection=selection)
    assert "- a" in out
    assert "- b" not in out
    assert "search_memory" in out
    assert "Showing 1 of 2" in out
    assert "\n\n## Skills\n- s1\n- s2" in out


def test_compose_no_chat_skips_current_request() -> None:
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx, chat_messages=None)
    assert "You are TestBot." in out
    assert "## Current request" not in out
    assert "monkeybot harness" in out


def test_compose_last_message_user_skips_duplicate_task() -> None:
    ctx = _minimal_ctx()
    msgs = [Message(role="user", content=[Text(text="Hello")])]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "## Current request" not in out


def test_compose_stable_and_volatile_split() -> None:
    from monkeybot.core.prompts.prompt import (
        compose_stable_baseline,
        compose_volatile_tail,
    )

    ctx = _minimal_ctx(
        memory_index=["fact-a"],
        skills=[SkillRef(name="s1", description="d1")],
    )
    stable = compose_stable_baseline(ctx)
    volatile = compose_volatile_tail(ctx)
    assert "You are TestBot." in stable
    assert "monkeybot harness" in stable
    assert "\n\n## Memory index\n" not in stable
    assert "\n\n## Skills\n" not in stable
    assert "- fact-a" in volatile
    assert "\n\n## Skills\n- s1" in volatile
    assert compose_system_prompt(ctx) == f"{stable}{volatile}"


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


def test_memory_section_with_skills_block() -> None:
    ctx = _minimal_ctx(
        memory_index=["Note A"],
        skills=[SkillRef(name="s1", description="d1")],
    )
    out = compose_system_prompt(ctx)
    assert "## Memory index" in out
    assert "- Note A" in out
    assert "\n\n## Skills\n- s1" in out
    assert "list_skills" in out


def test_session_attachments_block_present_and_in_stable_prefix() -> None:
    ctx = _minimal_ctx(memory_index=["Note A"], skills=[SkillRef(name="s1", description="d1")])
    record = AttachmentRecord(
        attachment_id="att1",
        filename="report.pdf",
        mime_type="application/pdf",
        description="Q1 report",
        storage_path=".monkeybot/attachments/t1/att1",
    )
    out = compose_system_prompt(ctx, attachment_catalog=[record])
    assert "\n\n## Session attachments\n- att1 (report.pdf, application/pdf): Q1 report" in out
    stable, volatile = split_system_prompt_for_cache(out)
    assert "## Session attachments" in stable
    assert "## Session attachments" not in volatile


def test_no_attachment_catalog_omits_session_attachments_block() -> None:
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx, attachment_catalog=None)
    assert "## Session attachments" not in out


def test_task_truncation() -> None:
    ctx = _minimal_ctx()
    long_user = "x" * 9000
    msgs = [
        Message(role="user", content=[Text(text=long_user)]),
        Message(role="assistant", content=[Text(text="calling tool")]),
    ]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "…(truncated)" in out
    assert len(out) < len(long_user) + 5500


def test_compose_harness_reflects_sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx)
    assert "gateway host" in out

    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    out_s = compose_system_prompt(ctx)
    assert "OpenSandbox" in out_s


def test_compose_harness_reflects_emission_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_EMISSION_STYLE", raising=False)
    ctx = _minimal_ctx()
    assert "### Emission style (terse)" not in compose_system_prompt(ctx)

    monkeypatch.setenv("MONKEYBOT_EMISSION_STYLE", "terse")
    out = compose_system_prompt(ctx)
    assert "### Emission style (terse)" in out
    # No task tool in the minimal ctx, so the agent-to-agent sub-block stays out.
    assert "### Subagent handoffs (dense)" not in out


def test_compose_emission_agent_to_agent_block_gated_on_task_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONKEYBOT_EMISSION_STYLE", "terse")
    ctx = _minimal_ctx(
        tools=[ToolDef("task", "subagent", {"type": "object", "properties": {}, "required": []})],
    )
    out = compose_system_prompt(ctx)
    assert "### Subagent handoffs (dense)" in out
    # Emission block lives in the stable (cacheable) prefix, before volatile sections.
    stable, _ = split_system_prompt_for_cache(out)
    assert "### Emission style (terse)" in stable
    assert "### Subagent handoffs (dense)" in stable


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
    assert out.index("## Current request") > out.index("monkeybot harness")


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
    stable_a, _ = split_system_prompt_for_cache(out_a)
    stable_b, _ = split_system_prompt_for_cache(out_b)
    assert "monkeybot harness" in stable_a
    assert stable_a == stable_b


def test_harness_precedes_volatile_sections() -> None:
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
    stable, volatile = split_system_prompt_for_cache(out)
    assert "monkeybot harness" in stable
    assert "## Memory index" not in stable
    assert "## Current request" not in stable
    mem_idx = volatile.index("## Memory index")
    skills_idx = volatile.index("## Skills")
    current_idx = volatile.index("## Current request")
    assert mem_idx < skills_idx < current_idx


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
