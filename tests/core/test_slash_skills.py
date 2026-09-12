"""Leading `/skill` invocation resolution and prompt injection."""

from dataclasses import replace

from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.context.slash_skills import apply_invoked_skill, resolve_invoked_skill
from monkeybot.core.llm.provider import Message
from monkeybot.core.prompts.prompt import compose_system_prompt, compose_volatile_tail_parts
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.types_tools import ToolDef


def _ctx(*, skills: list[SkillRef] | None = None, invoked: SkillRef | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="You are TestBot.",
        memory_index=[],
        skills=skills or [],
        tools=[ToolDef("list_skills", "List skills.", {})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        invoked_skill=invoked,
    )


BROWSER = SkillRef(name="browser", description="Control a browser")
IMAGE = SkillRef(name="image-generator", description="Generate images")
SKILLS = [BROWSER, IMAGE]


def test_resolve_matches_installed_skill_only() -> None:
    assert resolve_invoked_skill("/browser now", SKILLS) is BROWSER
    assert resolve_invoked_skill("/Browser now", SKILLS) is BROWSER
    assert resolve_invoked_skill("please /browser now", SKILLS) is BROWSER
    assert resolve_invoked_skill("/loop now", SKILLS) is None
    assert resolve_invoked_skill("/not-a-skill", SKILLS) is None
    assert resolve_invoked_skill("browser please", SKILLS) is None


def test_apply_invoked_skill_is_turn_scoped() -> None:
    ctx = _ctx(skills=SKILLS)
    next_ctx = apply_invoked_skill(ctx, "/browser open the account page")
    assert next_ctx.invoked_skill is BROWSER
    assert ctx.invoked_skill is None
    unchanged = apply_invoked_skill(ctx, "/missing do it")
    assert unchanged is ctx


def test_prompt_includes_invoked_skill_and_keeps_user_text() -> None:
    user = "/browser open the account page"
    ctx = _ctx(skills=SKILLS)
    next_ctx = apply_invoked_skill(ctx, user)
    out = compose_system_prompt(
        next_ctx,
        chat_messages=[Message(role="user", content=[Text(text=user)])],
    )
    assert "## Invoked skill\nThe user explicitly invoked `browser`." in out
    assert "skills/browser/SKILL.md" in out
    parts = compose_volatile_tail_parts(
        next_ctx,
        chat_messages=[Message(role="user", content=[Text(text=user)])],
    )
    assert "browser" in parts["invoked_skill"]
    # History stays the original command text; only turn context is annotated.
    assert user == "/browser open the account page"
    assert next_ctx.agent_md == ctx.agent_md
    assert next_ctx.skills == ctx.skills


def test_prompt_omits_invoked_skill_without_match() -> None:
    ctx = apply_invoked_skill(_ctx(skills=SKILLS), "/loop now")
    out = compose_system_prompt(ctx)
    assert "## Invoked skill" not in out
    assert ctx.invoked_skill is None


def test_invoked_skill_survives_tool_round_history() -> None:
    ctx = replace(_ctx(skills=SKILLS), invoked_skill=BROWSER)
    msgs = [
        Message(role="user", content=[Text(text="/browser open the account page")]),
        Message(role="assistant", content=[Text(text="calling tool")]),
    ]
    parts = compose_volatile_tail_parts(ctx, chat_messages=msgs)
    assert "explicitly invoked `browser`" in parts["invoked_skill"]
