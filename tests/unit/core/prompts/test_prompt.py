"""Tests for :mod:`monkeybot.core.prompts.prompt`."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from monkeybot.core.attachments.catalog import AttachmentRecord
from monkeybot.core.context import SkillRef
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.headings import (
    BACKGROUND_TASKS_HEADING,
    CURRENT_DATE_HEADING,
    CURRENT_REQUEST_HEADING,
    MEMORY_INDEX_HEADING,
    SKILLS_HEADING,
    TODO_LIST_HEADING,
    heading_marker,
)
from monkeybot.core.prompts.prompt import (
    _MAX_CURRENT_REQUEST_CHARS,
    compose_stable_baseline,
    compose_stable_baseline_parts,
    compose_system_prompt,
    compose_volatile_tail_parts,
)
from monkeybot.core.prompts.section_budget import SECTION_CHAR_CAPS
from monkeybot.core.types.content_blocks import Text, ToolRequest, ToolResponse
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers._utils import split_system_prompt_for_cache
from monkeybot.todo_list.store import TodoItem, TodoListStore
from testkit.context import _minimal_ctx


def test_compose_omits_wake_up_and_teaching_when_memory_off() -> None:
    ctx = _minimal_ctx()
    assert ctx.memory is None
    out = compose_system_prompt(ctx)
    assert "## Memory wake-up" not in out
    assert "### Memory retrieval (`mempalace search`)" not in out
    assert 'argv: ["mempalace", "search"' not in out
    assert "do not call `mempalace search`" in out


def test_compose_uses_memory_index_lines() -> None:
    ctx = _minimal_ctx(
        memory_index=["a", "b"],
        skills=[
            SkillRef(name="s1", description="d1"),
            SkillRef(name="s2", description="d2"),
        ],
    )
    out = compose_system_prompt(ctx)
    mem_section = out.split("## Memory wake-up", 1)[1].split("## Skills", 1)[0]
    assert "a" in mem_section
    assert "b" in mem_section
    assert "\n\n## Skills\n- s1 — d1\n- s2 — d2" in out


def test_compose_retired_context_curation_does_not_truncate_wake_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config import reset_runtime_env_state_for_tests
    from monkeybot.core.config.snapshot import build_runtime_config

    reset_runtime_env_state_for_tests()
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "monkeybot.yaml").write_text(
        "context_curation:\n  enabled: true\n  memory_window_lines: 2\n",
        encoding="utf-8",
    )
    cfg = build_runtime_config(agent_root=tmp_path)
    ctx = replace(
        _minimal_ctx(memory_index=["l1", "l2", "l3", "l4"]),
        config=cfg,
    )
    out = compose_system_prompt(ctx)
    mem_section = out.split("## Memory wake-up", 1)[1].split("## Skills", 1)[0]
    assert "l1" in mem_section
    assert "l2" in mem_section
    assert "l3" in mem_section
    assert "l4" in mem_section
    reset_runtime_env_state_for_tests()


def test_compose_no_chat_skips_current_request() -> None:
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx, chat_messages=None)
    assert "You are TestBot." in out
    assert "## Current request" not in out
    assert "monkeybot harness" in out
    assert f"## Current date\n{date.today().isoformat()}" in out


def test_compose_last_message_user_skips_duplicate_task() -> None:
    ctx = _minimal_ctx()
    msgs = [Message(role="user", content=[Text(text="Hello")])]
    out = compose_system_prompt(ctx, chat_messages=msgs)
    assert "## Current request" not in out


def test_compose_current_date_is_volatile_yyyy_mm_dd() -> None:
    from monkeybot.core.prompts.prompt import (
        compose_stable_baseline,
        compose_volatile_tail,
        compose_volatile_tail_parts,
    )

    ctx = _minimal_ctx()
    today = date.today().isoformat()
    stable = compose_stable_baseline(ctx)
    volatile = compose_volatile_tail(ctx)
    parts = compose_volatile_tail_parts(ctx)

    assert "## Current date" not in stable
    assert parts["current_date"] == f"\n\n## Current date\n{today}"
    assert volatile.startswith(parts["current_date"])
    split_stable, split_volatile = split_system_prompt_for_cache(f"{stable}{volatile}")
    assert "## Current date" not in split_stable
    assert split_volatile.startswith(f"\n\n## Current date\n{today}")


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
    assert "\n\n## Current date\n" not in stable
    assert "\n\n## Memory wake-up\n" not in stable
    assert "\n\n## Skills\n" not in stable
    assert f"\n\n## Current date\n{date.today().isoformat()}" in volatile
    assert "fact-a" in volatile
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
    assert "## Memory wake-up" in out
    assert "Note A" in out
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
    # What matters is that the injected request body is capped, so measure only
    # what the paste added rather than the whole prompt — otherwise ordinary
    # harness growth fails this test for the wrong reason (it did).
    added = len(out) - len(compose_system_prompt(ctx, chat_messages=[]))
    assert added < len(long_user)
    assert added <= _MAX_CURRENT_REQUEST_CHARS + len(CURRENT_REQUEST_HEADING) + 200


def test_compose_harness_reflects_sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
    ctx = _minimal_ctx()
    out = compose_system_prompt(ctx)
    assert "gateway host" in out

    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    out_s = compose_system_prompt(ctx)
    assert "OpenSandbox" in out_s


def test_compose_harness_sandbox_follows_pinned_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.config import reset_runtime_env_state_for_tests
    from monkeybot.core.config.snapshot import build_runtime_config

    reset_runtime_env_state_for_tests()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "monkeybot.yaml").write_text(
        "sandbox:\n  enabled: true\n",
        encoding="utf-8",
    )
    cfg = build_runtime_config(agent_root=tmp_path)
    monkeypatch.setenv("SANDBOX_ENABLED", "false")
    ctx = replace(_minimal_ctx(), config=cfg)
    assert "OpenSandbox" in compose_system_prompt(ctx)
    reset_runtime_env_state_for_tests()


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
    # Match heading markers, not bare titles: the harness prose cross-references
    # sections by name (e.g. "entries under `## Memory index` are titles only"),
    # so a substring check would flag those references as a split failure.
    for heading in (CURRENT_DATE_HEADING, MEMORY_INDEX_HEADING, CURRENT_REQUEST_HEADING):
        assert heading_marker(heading) not in stable
    date_idx = volatile.index(heading_marker(CURRENT_DATE_HEADING))
    mem_idx = volatile.index(heading_marker(MEMORY_INDEX_HEADING))
    skills_idx = volatile.index(heading_marker(SKILLS_HEADING))
    current_idx = volatile.index(heading_marker(CURRENT_REQUEST_HEADING))
    assert date_idx < mem_idx < skills_idx < current_idx


def test_skills_section_lists_description_and_handles_edge_cases() -> None:
    long_desc = "word " * 100
    ctx = _minimal_ctx(
        skills=[
            SkillRef(name="plain", description="Does a thing\nacross lines"),
            SkillRef(name="bare", description=""),
            SkillRef(name="wordy", description=long_desc),
        ],
    )
    out = compose_system_prompt(ctx)
    skills_block = out.split("\n\n## Skills\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert skills_block[0] == "- plain — Does a thing across lines"
    assert skills_block[1] == "- bare"
    assert skills_block[2].startswith("- wordy — word word")
    assert skills_block[2].endswith("…")
    assert len(skills_block[2]) < 230


def _todo_body(ctx: object) -> str:
    from monkeybot.core.context import TurnContext

    assert isinstance(ctx, TurnContext)
    section = compose_volatile_tail_parts(ctx)["todos"]
    assert section.startswith(TODO_LIST_HEADING)
    return section[len(TODO_LIST_HEADING) :]


def _store_with(tmp_path: Path, items: list[TodoItem]) -> TodoListStore:
    store = TodoListStore("todos", workspace_root=tmp_path, mirror_to_disk=False)
    store._items = items
    return store


def test_todo_section_matches_the_store_when_under_the_cap(tmp_path: Path) -> None:
    store = _store_with(
        tmp_path,
        [
            TodoItem(id="t1", text="Write the failing test", status="pending"),
            TodoItem(id="t2", text="Make it pass", status="in_progress"),
        ],
    )
    body = _todo_body(replace(_minimal_ctx(), todo_store=store))
    assert body == store.format_lines()


def test_todo_section_keeps_every_open_id_inside_the_cap(tmp_path: Path) -> None:
    text = "x" * 500
    items = [TodoItem(id="t1", text=text, status="in_progress")]
    items.extend(TodoItem(id=f"t{i}", text=text, status="pending") for i in range(2, 51))
    store = _store_with(tmp_path, items)
    body = _todo_body(replace(_minimal_ctx(), todo_store=store))
    assert len(body) <= SECTION_CHAR_CAPS["todos"]
    for item in items:
        assert f"- {item.id} [" in body
    assert f"- t1 [in_progress] {text}" in body
    assert "Long items are clipped. Start an item to see its full text." in body


def test_todo_section_collapses_done_items(tmp_path: Path) -> None:
    items = [TodoItem(id=f"t{i}", text="y" * 500, status="done") for i in range(1, 41)]
    items.append(TodoItem(id="t41", text="keep me", status="pending"))
    store = _store_with(tmp_path, items)
    body = _todo_body(replace(_minimal_ctx(), todo_store=store))
    assert len(body) <= SECTION_CHAR_CAPS["todos"]
    assert "- done (40): t1, t2," in body
    assert "t40" in body
    assert "keep me" in body
    assert "y" * 500 not in body
    assert "Long items are clipped" not in body


def test_todo_section_three_hundred_items_stay_inside_the_cap(tmp_path: Path) -> None:
    items = [TodoItem(id=f"t{i}", text="z" * 500, status="pending") for i in range(1, 301)]
    store = _store_with(tmp_path, items)
    body = _todo_body(replace(_minimal_ctx(), todo_store=store))
    assert len(body) <= SECTION_CHAR_CAPS["todos"]
    assert "Long items are clipped" in body


def test_background_tasks_section_is_capped() -> None:
    huge = "x" * 5_000
    ctx = replace(_minimal_ctx(), background_runs_view=lambda: huge)
    section = compose_volatile_tail_parts(ctx)["background_tasks"]
    assert section.startswith(BACKGROUND_TASKS_HEADING)
    body = section[len(BACKGROUND_TASKS_HEADING) :]
    assert body.endswith("\n")
    assert len(body.strip()) <= SECTION_CHAR_CAPS["background_tasks"]
    assert "… [truncated]" in body

    short = replace(_minimal_ctx(), background_runs_view=lambda: "one line")
    assert compose_volatile_tail_parts(short)["background_tasks"] == (
        f"{BACKGROUND_TASKS_HEADING}one line\n"
    )


def test_stable_parts_join_equals_the_baseline() -> None:
    ctx = replace(
        _minimal_ctx(),
        project_instructions="Be careful.",
        repo_overview="Layout (workspace root):\n  src/",
    )
    record = AttachmentRecord(
        attachment_id="att1",
        filename="report.pdf",
        mime_type="application/pdf",
        description="Q1 report",
        storage_path=".monkeybot/attachments/t1/att1",
    )
    parts = compose_stable_baseline_parts(ctx, attachment_catalog=[record])
    assert "".join(parts.values()) == compose_stable_baseline(ctx, attachment_catalog=[record])
    assert list(parts) == [
        "agent_md",
        "harness",
        "project_instructions",
        "repo_overview",
        "attachments",
    ]


def test_coding_protocol_is_always_in_stable_prefix() -> None:
    marker = "### Editing & verification protocol"
    for ctx in (
        _minimal_ctx(),  # no pinned config at all
        _minimal_ctx(skills=[SkillRef(name="s1", description="d1")]),
    ):
        stable, volatile = split_system_prompt_for_cache(compose_system_prompt(ctx))
        assert marker in stable
        assert marker not in volatile
        assert stable.index(marker) < stable.index("### Tool-call protocol (strict)")
