"""AGENT.md and the skill folders are read off the event loop."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from monkeybot.core.context import _discover_skills, _load_agent_md, build_context
from testkit.mcp import FakeMCPClient

_SKILL_MD = """---
name: demo
description: A skill used to check discovery.
---

# demo
"""


async def test_build_context_reads_agent_and_skills_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two reads run on a worker, and the turn still sees the file text."""
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("You are a test agent\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skill_dir = skills / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")

    seen: list[tuple[str, int]] = []
    loop_thread = threading.get_ident()
    real_load = _load_agent_md
    real_discover = _discover_skills

    def load(path: Path) -> str:
        seen.append(("agent", threading.get_ident()))
        return real_load(path)

    def discover(path: Path) -> list[object]:
        seen.append(("skills", threading.get_ident()))
        return real_discover(path)

    monkeypatch.setattr("monkeybot.core.context._load_agent_md", load)
    monkeypatch.setattr("monkeybot.core.context._discover_skills", discover)

    ctx = await build_context(
        "thread",
        "request",
        agent_md_path=agent_path,
        memory=None,
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )

    assert ctx.agent_md == real_load(agent_path)
    assert [(skill.name, skill.description) for skill in ctx.skills] == [
        (skill.name, skill.description) for skill in real_discover(skills)
    ]
    assert [name for name, _thread in seen] == ["agent", "skills"]
    assert all(thread != loop_thread for _name, thread in seen)
