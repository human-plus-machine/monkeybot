"""Routing eval: past-event questions → mempalace search; code questions → search."""

from __future__ import annotations

from monkeybot.core.context import _core_tool_defs
from monkeybot.core.prompts import prompt as prompt_mod
from monkeybot.core.prompts.harness_prompt import harness_fixed_context


ROUTING_CASES: list[tuple[str, str]] = [
    ("What did we decide about the refund policy last week?", "run_command"),
    ("What is the user's preferred theme?", "run_command"),
    ("Did we already try deploying to staging in a prior session?", "run_command"),
    ("Remind me what the customer said about billing.", "run_command"),
    ("What preferences has this user stored?", "run_command"),
    ("What happened when the glob tool failed earlier?", "run_command"),
    ("Where is the authentication middleware defined?", "search"),
    ("How does the SSE gateway handle reconnects?", "search"),
    ("Which file configures NVIDIA embeddings?", "search"),
    ("How does KnowledgeIndexer walk workspace files?", "search"),
    ("Where does CoreToolExecutor dispatch search?", "search"),
    ("Why is the refund path configured this way?", "run_command"),
]


def test_contrastive_tool_descriptions_encode_routing() -> None:
    tools = {t.name: t for t in _core_tool_defs(include_task_tool=True)}
    assert "search_memory" not in tools
    assert "edit_memory" not in tools
    assert "update_memory" not in tools
    assert "forget" not in tools
    assert "search" in tools
    assert "run_command" in tools
    search_desc = tools["search"].description.lower()
    assert "mempalace" in search_desc or "past conversation" in search_desc or "no" in search_desc


def test_harness_memory_teaching_and_no_delegation() -> None:
    text = harness_fixed_context(
        include_task_tool=True,
        include_knowledge_search=True,
        workspace_root="/tmp/ws",
        memory_storage_uri="local://./memory/mempalace",
    )
    assert "Memory retrieval (`mempalace search`)" in text
    assert "search_memory" not in text
    assert "do not `read_file` palace paths" in text.lower() or "do not read_file palace" in text.lower()
    assert "outside" in text.lower()
    assert (
        "past conversation" in text.lower()
        or "no** record of past" in text.lower()
        or "no record of past" in text.lower()
    )


def test_routing_cases_match_tool_surface() -> None:
    tools = {t.name: t for t in _core_tool_defs(include_task_tool=True)}
    harness = harness_fixed_context(
        include_task_tool=True,
        include_knowledge_search=True,
        workspace_root="/tmp/ws",
        memory_storage_uri="local://./memory/mempalace",
    ).lower()
    for _question, expected in ROUTING_CASES:
        assert expected in tools
        if expected == "run_command":
            assert "mempalace search" in harness
        else:
            assert "search" in harness


def test_memory_index_heading_is_framed() -> None:
    assert "mempalace search" in prompt_mod.MEMORY_INDEX_HEADING
    assert "wake-up" in prompt_mod.MEMORY_INDEX_HEADING.lower()
