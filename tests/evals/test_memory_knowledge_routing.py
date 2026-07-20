"""Routing eval: past-event questions → search_memory; code questions → search."""

from __future__ import annotations

from monkeybot.core.context import _core_tool_defs
from monkeybot.core.context.memory_prompt import MemoryPromptSelection
from monkeybot.core.prompts import prompt as prompt_mod
from monkeybot.core.prompts.harness_prompt import harness_fixed_context


# Fixed questions with the expected first-choice tool surface.
ROUTING_CASES: list[tuple[str, str]] = [
    # Past / preference / session → search_memory
    ("What did we decide about the refund policy last week?", "search_memory"),
    ("What is the user's preferred theme?", "search_memory"),
    ("Did we already try deploying to staging in a prior session?", "search_memory"),
    ("Remind me what the customer said about billing.", "search_memory"),
    ("What preferences has this user stored?", "search_memory"),
    ("What happened when the glob tool failed earlier?", "search_memory"),
    # Code / workspace → search
    ("Where is the authentication middleware defined?", "search"),
    ("How does the SSE gateway handle reconnects?", "search"),
    ("Which file configures NVIDIA embeddings?", "search"),
    ("How does KnowledgeIndexer walk workspace files?", "search"),
    ("Where does CoreToolExecutor dispatch search_memory?", "search"),
    # Ambiguous "why" — memory first is acceptable (plan)
    ("Why is the refund path configured this way?", "search_memory"),
]


def _expected_surface_from_tool_text(question: str, tool_name: str, description: str) -> bool:
    """Heuristic: question domain cues + tool description must agree on routing."""
    q = question.lower()
    desc = description.lower()
    if tool_name == "search_memory":
        question_signals = (
            "decide",
            "decided",
            "prefer",
            "preferred",
            "last week",
            "prior",
            "session",
            "customer said",
            "remind",
            "preferences",
            "happened",
            "earlier",
            "why is",
            "user's",
            "user stored",
        )
        memory_desc = (
            "past",
            "preference",
            "session",
            "decision",
            "not for code",
            "prior",
        )
        return any(s in q for s in question_signals) and any(
            s in desc for s in memory_desc
        )
    if tool_name == "search":
        question_signals = (
            "where is",
            "how does",
            "which file",
            "middleware",
            "gateway",
            "embeddings",
            "indexer",
            "executor",
            "defined",
            "configures",
            "walk",
            "dispatch",
        )
        code_desc = (
            "workspace",
            "code",
            "no record of past",
            "conversation",
            "cross-file",
        )
        return any(s in q for s in question_signals) and any(s in desc for s in code_desc)
    return False


def test_contrastive_tool_descriptions_encode_routing() -> None:
    tools = {t.name: t for t in _core_tool_defs(include_task_tool=True)}
    assert "search_memory" in tools
    assert "search" in tools
    mem_desc = tools["search_memory"].description.lower()
    search_desc = tools["search"].description.lower()
    assert "not for code" in mem_desc or "workspace" in mem_desc
    assert "read_file" in mem_desc
    assert "path" in mem_desc
    assert "search_memory" in search_desc or "past conversation" in search_desc
    assert "edit_memory" in tools
    assert "update_memory" in tools
    assert "forget" in tools
    schema_props = tools["search_memory"].input_schema["properties"]
    assert "path" in schema_props
    assert "query" in schema_props


def test_harness_memory_teaching_and_no_delegation() -> None:
    text = harness_fixed_context(
        include_task_tool=True,
        include_knowledge_search=True,
        workspace_root="/tmp/ws",
        memory_storage_uri="local://./memory",
    )
    assert "delegates to `search`" not in text
    assert "Never delegates to knowledge search" in text or "never delegates" in text.lower()
    assert "Memory retrieval (`search_memory`)" in text
    assert "what happened" in text.lower()
    assert "never `read_file`" in text.lower() or "never use `read_file`" in text.lower() or "do **not** use `read_file`" in text.lower()
    assert "path" in text.lower()
    assert "outside" in text.lower()
    assert "../memory" in text
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
        memory_storage_uri="local://./memory",
    ).lower()
    for question, expected in ROUTING_CASES:
        desc = tools[expected].description
        assert _expected_surface_from_tool_text(question, expected, desc), (
            f"tool {expected} description does not cover routing for: {question!r}"
        )
        if expected == "search_memory":
            assert "search_memory" in harness
        else:
            assert "search" in harness


def test_memory_index_heading_is_framed() -> None:
    assert "search_memory" in prompt_mod.MEMORY_INDEX_HEADING
    assert (
        "titles" in prompt_mod.MEMORY_INDEX_HEADING.lower()
        or "full note" in prompt_mod.MEMORY_INDEX_HEADING.lower()
    )


def test_curator_nudge_prefers_search_memory_for_session_context() -> None:
    class _Ctx:
        memory_index = ["- [[semantic/a.md]] | tags: | summary: dark mode"]

    sel = MemoryPromptSelection(
        lines=["- [[semantic/a.md]] | tags: | summary: dark mode"],
        total_lines=10,
        coverage=0.1,
        confidence=0.5,
        nudge_search=True,
        use_custom_lines=True,
    )
    block = prompt_mod._memory_block(_Ctx(), memory_selection=sel)  # type: ignore[arg-type]
    assert "search_memory" in block
    assert "Use `search` (or `search_memory`)" not in block
