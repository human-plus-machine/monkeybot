"""Per-section prompt size on the system-prompt snapshot."""

from dataclasses import replace
from pathlib import Path

import pytest

from monkeybot.core.context.epoch import ContextEpochTracker, fingerprint_text
from monkeybot.core.prompts.prompt import (
    compose_stable_baseline_parts,
    compose_volatile_tail_parts,
)
from monkeybot.core.prompts.section_budget import SECTION_CHAR_CAPS
from monkeybot.core.runtime.context_budget import estimate_tokens_from_char_count
from monkeybot.core.runtime.loop_messages import (
    _append_extra_system_text,
    _system_message_from_text,
    _system_prompt_snapshot_text,
    build_prompt_sections,
)
from monkeybot.core.types.content_blocks import Text
from monkeybot.todo_list.store import TodoItem, TodoListStore
from testkit.context import _minimal_ctx


def _admit(
    ctx: object, tracker: ContextEpochTracker | None = None
) -> tuple[ContextEpochTracker, object]:
    from monkeybot.core.context import TurnContext
    from monkeybot.core.context.epoch import EpochAdmit

    assert isinstance(ctx, TurnContext)
    tracker = tracker or ContextEpochTracker()
    stable_parts = compose_stable_baseline_parts(ctx)
    volatile = compose_volatile_tail_parts(ctx)
    stable = "".join(stable_parts.values())
    volatile_text = "".join(volatile.values())
    admit = tracker.reconcile(
        stable_baseline=stable,
        volatile_text=volatile_text,
        stable_fingerprint=fingerprint_text(stable),
        volatile_fingerprint=fingerprint_text(volatile_text),
        volatile_parts=volatile,
        stable_parts=stable_parts,
    )
    assert isinstance(admit, EpochAdmit)
    return tracker, admit


def _store(tmp_path: Path, items: list[TodoItem]) -> TodoListStore:
    store = TodoListStore("sections", workspace_root=tmp_path, mirror_to_disk=False)
    store._items = items
    return store


def test_sections_are_present_and_sum_to_the_snapshot(tmp_path: Path) -> None:
    from monkeybot.core.context.epoch import EpochAdmit

    ctx = replace(
        _minimal_ctx(),
        project_instructions="Be careful.",
        todo_store=_store(tmp_path, [TodoItem(id="t1", text="ship it", status="pending")]),
    )
    _tracker, admit = _admit(ctx)
    assert isinstance(admit, EpochAdmit)
    notes = "Remember the failing test."
    system = _append_extra_system_text(_system_message_from_text(admit.leading_system_text), notes)
    snapshot = _system_prompt_snapshot_text(system, admit.context_tail)
    sections = build_prompt_sections(
        stable_parts=admit.stable_parts,
        volatile_parts=admit.leading_volatile_parts,
        runtime_notes=notes,
        context_tail=admit.context_tail,
        tools_chars=120,
        snapshot_text=snapshot,
    )
    names = {section.name for section in sections}
    assert {
        "agent_md",
        "harness",
        "project_instructions",
        "todos",
        "runtime_notes",
        "tools",
    } <= names
    assert "unattributed" not in names
    assert sum(section.chars for section in sections if section.name != "tools") == len(snapshot)
    todos = next(section for section in sections if section.name == "todos")
    assert todos.cap_chars == SECTION_CHAR_CAPS["todos"]
    assert todos.est_tokens == estimate_tokens_from_char_count(todos.chars)
    tools = next(section for section in sections if section.name == "tools")
    assert tools.chars == 120
    assert tools.cap_chars is None


def test_runtime_notes_over_the_cap_keep_the_head_and_the_tail() -> None:
    cap = SECTION_CHAR_CAPS["runtime_notes"]
    extra = "HEAD-" + ("n" * (cap + 40)) + "-TAIL"
    system = _append_extra_system_text(_system_message_from_text("BASE"), extra)
    block = system.content[0]
    assert isinstance(block, Text)
    body = block.text.split("## Runtime notes\n\n", 1)[1].removesuffix("\n")
    assert len(body) == cap
    assert body.startswith("HEAD-")
    assert body.endswith("-TAIL")
    assert "… [runtime notes truncated]" in body


def test_report_keeps_the_baseline_todo_list(tmp_path: Path) -> None:
    from monkeybot.core.context.epoch import EpochAdmit

    store = _store(tmp_path, [TodoItem(id="t1", text="first", status="pending")])
    ctx = replace(_minimal_ctx(), todo_store=store)
    tracker, _first = _admit(ctx)
    store._items = [TodoItem(id="t1", text="x" * 500, status="pending")]
    _tracker, second = _admit(ctx, tracker)
    assert isinstance(second, EpochAdmit)
    assert second.kind == "volatile_updated"
    assert "first" in second.leading_volatile_parts["todos"]
    assert "x" * 500 not in second.leading_volatile_parts["todos"]
    assert "x" * 500 in second.context_tail


def test_a_rewritten_system_message_is_unattributed() -> None:
    from monkeybot.core.context.epoch import EpochAdmit

    _tracker, admit = _admit(_minimal_ctx())
    assert isinstance(admit, EpochAdmit)
    system = _system_message_from_text(admit.leading_system_text)
    composed = _system_prompt_snapshot_text(system, "")
    rewritten = composed + "\nEXTRA"
    sections = build_prompt_sections(
        stable_parts=admit.stable_parts,
        volatile_parts=admit.leading_volatile_parts,
        runtime_notes=None,
        context_tail="",
        tools_chars=2,
        snapshot_text=rewritten,
    )
    gap = next(section for section in sections if section.name == "unattributed")
    assert gap.chars == len("\nEXTRA")
    assert sum(section.chars for section in sections if section.name != "tools") == len(rewritten)

    replaced = build_prompt_sections(
        stable_parts=admit.stable_parts,
        volatile_parts=admit.leading_volatile_parts,
        runtime_notes=None,
        context_tail="",
        tools_chars=2,
        snapshot_text="not the composed prompt",
    )
    assert [section.name for section in replaced] == ["unattributed", "tools"]


@pytest.mark.asyncio
async def test_loop_snapshot_sections_sum_to_the_text() -> None:
    from monkeybot.core.llm.provider import Done, TextDelta
    from monkeybot.core.runtime.events import SystemPromptSnapshot
    from monkeybot.core.runtime.loop import run
    from testkit.context import _ctx
    from testkit.executors import RecordingExecutor
    from testkit.history import FakeHistory
    from testkit.provider import FakeProvider

    events = []
    async for event in run(
        "hello",
        _ctx(),
        provider=FakeProvider([[TextDelta(text="hi"), Done()]]),
        history=FakeHistory(),
        inspectors=[],
        tool_executor=RecordingExecutor(),
        max_turns=2,
    ):
        events.append(event)
    snaps = [event for event in events if isinstance(event, SystemPromptSnapshot)]
    assert snaps
    snap = snaps[0]
    assert snap.sections
    assert sum(section.chars for section in snap.sections if section.name != "tools") == len(
        snap.text
    )
    assert any(section.name == "tools" and section.chars > 0 for section in snap.sections)
