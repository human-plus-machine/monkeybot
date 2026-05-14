from __future__ import annotations

import pytest

from monkeybot.core.context import SkillRef, TurnContext, load_turn_context

# ---------------------------------------------------------------------------
# load_turn_context
# ---------------------------------------------------------------------------


def test_load_turn_context_fully_populated(tmp_path):
    """All three dirs populated → correct counts."""
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# Agent\nDo stuff")

    memory_path = tmp_path / "memory"
    memory_path.mkdir()
    (memory_path / "note1.md").write_text("# Note One\nfirst line of note1")
    (memory_path / "note2.md").write_text("# Note Two\nfirst line of note2")

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    skill_a = skills_path / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text("# Skill A\n\nThis is skill a description.")
    skill_b = skills_path / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text("# Skill B\n\nThis is skill b description.")

    ctx = load_turn_context(str(agent_md), str(memory_path), str(skills_path))

    assert len(ctx.memory_index) == 2
    assert len(ctx.skills) == 2
    assert ctx.memory_path == str(memory_path.resolve())
    assert ctx.skills_path == str(skills_path.resolve())


def test_load_turn_context_missing_memory_path(tmp_path):
    """Missing memory_path → memory_index == [], no exception."""
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# Agent")

    ctx = load_turn_context(
        str(agent_md),
        str(tmp_path / "nonexistent_memory"),
        str(tmp_path / "nonexistent_skills"),
    )
    assert ctx.memory_index == []


def test_load_turn_context_missing_skills_path(tmp_path):
    """Missing skills_path → skills == [], no exception."""
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("# Agent")

    memory_path = tmp_path / "memory"
    memory_path.mkdir()

    ctx = load_turn_context(
        str(agent_md),
        str(memory_path),
        str(tmp_path / "nonexistent_skills"),
    )
    assert ctx.skills == []


def test_load_turn_context_missing_agent_md_raises(tmp_path):
    """Missing agent_md_path → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_turn_context(
            str(tmp_path / "missing.md"),
            str(tmp_path / "memory"),
            str(tmp_path / "skills"),
        )


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


def test_build_system_prompt_with_memory_and_skills():
    """Prompt contains Memory Index and Available Skills sections."""
    skill = SkillRef(name="my-skill", description="Does things", path="/skills/my-skill/SKILL.md")
    ctx = TurnContext(
        agent_md="You are an agent.",
        memory_index=["note1: first line"],
        skills=[skill],
        memory_path="/mem",
        skills_path="/skills",
    )
    prompt = ctx.build_system_prompt()
    assert "## Memory Index" in prompt
    assert "## Available Skills" in prompt
    assert "note1: first line" in prompt
    assert "my-skill" in prompt


def test_build_system_prompt_empty_memory_and_skills():
    """With no memory and no skills, prompt equals agent_md exactly."""
    ctx = TurnContext(
        agent_md="You are an agent.",
        memory_index=[],
        skills=[],
        memory_path="/mem",
        skills_path="/skills",
    )
    prompt = ctx.build_system_prompt()
    assert prompt == "You are an agent."


def test_build_system_prompt_only_memory():
    """Memory section present, no skills section."""
    ctx = TurnContext(
        agent_md="# Agent",
        memory_index=["doc: some content"],
        skills=[],
        memory_path="/mem",
        skills_path="/skills",
    )
    prompt = ctx.build_system_prompt()
    assert "## Memory Index" in prompt
    assert "## Available Skills" not in prompt


def test_build_system_prompt_only_skills():
    """Skills section present, no memory section."""
    skill = SkillRef(name="s1", description="A skill", path="/p")
    ctx = TurnContext(
        agent_md="# Agent",
        memory_index=[],
        skills=[skill],
        memory_path="/mem",
        skills_path="/skills",
    )
    prompt = ctx.build_system_prompt()
    assert "## Available Skills" in prompt
    assert "## Memory Index" not in prompt
    assert "list_skills()" in prompt


def test_build_system_prompt_substitutes_memory_path_placeholder():
    """{MEMORY_PATH} in AGENT.md is replaced with the real memory directory."""
    ctx = TurnContext(
        agent_md="Save notes under {MEMORY_PATH}/notes.md",
        memory_index=[],
        skills=[],
        memory_path="/abs/data/memory",
        skills_path="/abs/skills",
    )
    prompt = ctx.build_system_prompt()
    assert "{MEMORY_PATH}" not in prompt
    assert "Save notes under /abs/data/memory/notes.md" in prompt


# ---------------------------------------------------------------------------
# Provider protocol smoke test (Task 3)
# ---------------------------------------------------------------------------


def test_fake_provider_satisfies_protocol():
    from monkeybot.core.provider import Provider

    class FakeProvider:
        name = "fake"
        supports_streaming = True

        async def stream(self, messages, tools, *, model, system, context=None):
            async def _gen():
                yield  # type: ignore

            return _gen()

    assert isinstance(FakeProvider(), Provider)


# ---------------------------------------------------------------------------
# Inspector tests (Task 4)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx():
    return TurnContext(
        agent_md="",
        memory_index=[],
        skills=[],
        memory_path="/mem",
        skills_path="/skills",
    )


async def test_command_tier_deny(ctx: TurnContext) -> None:
    from monkeybot.core.inspector import CommandTierInspector
    from monkeybot.core.provider import ToolCall

    inspector = CommandTierInspector({"denied": ["rm_all"]})
    decision = await inspector.check(ToolCall("c1", "rm_all", {}), ctx)
    assert decision.kind == "deny"


async def test_command_tier_approve(ctx: TurnContext) -> None:
    from monkeybot.core.inspector import CommandTierInspector
    from monkeybot.core.provider import ToolCall

    inspector = CommandTierInspector({"requires_approval": ["deploy"]})
    decision = await inspector.check(ToolCall("c1", "deploy", {}), ctx)
    assert decision.kind == "approve"


async def test_command_tier_allow(ctx: TurnContext) -> None:
    from monkeybot.core.inspector import CommandTierInspector
    from monkeybot.core.provider import ToolCall

    inspector = CommandTierInspector({})
    decision = await inspector.check(ToolCall("c1", "echo", {}), ctx)
    assert decision.kind == "allow"


async def test_rules_inspector_deny(ctx: TurnContext) -> None:
    from monkeybot.core.inspector import RulesInspector
    from monkeybot.core.provider import ToolCall

    inspector = RulesInspector(["sudo"])
    decision = await inspector.check(
        ToolCall("c1", "run_command", {"command": "sudo rm"}), ctx
    )
    assert decision.kind == "deny"


async def test_rules_inspector_allow(ctx: TurnContext) -> None:
    from monkeybot.core.inspector import RulesInspector
    from monkeybot.core.provider import ToolCall

    inspector = RulesInspector(["sudo"])
    decision = await inspector.check(
        ToolCall("c1", "run_command", {"command": "ls"}), ctx
    )
    assert decision.kind == "allow"
