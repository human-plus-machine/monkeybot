"""Turn-context builders shared across test modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.types.types_tools import ToolDef


def _ctx(
    *,
    workspace_root: Path | None = None,
    context_window_tokens: int = 200_000,
    model: str = "gemini-2.5-flash",
    summarization_model: str | None = None,
) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("run_command", "Run shell", {})],
        user_id=None,
        parent_run_id=None,
        model=model,
        summarization_model=summarization_model,
        workspace_root=workspace_root,
        context_window_tokens=context_window_tokens,
    )


def _ctx_with_task(
    *,
    workspace_root: Path | None = None,
    context_window_tokens: int = 200_000,
    model: str = "gemini-2.5-flash",
    summarization_model: str | None = None,
) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("task", "Spawn subagent", {}),
            ToolDef("run_command", "Run shell", {}),
        ],
        user_id=None,
        parent_run_id=None,
        model=model,
        summarization_model=summarization_model,
        workspace_root=workspace_root,
        context_window_tokens=context_window_tokens,
    )


def tool_ctx(
    skills: list[SkillRef] | None = None,
    *,
    event_publisher: object | None = None,
) -> TurnContext:
    kwargs: dict[str, object] = {
        "thread_id": "t",
        "request_id": "r",
        "agent_md": "# Agent",
        "memory_index": [],
        "skills": skills or [],
        "tools": [],
        "user_id": None,
        "parent_run_id": None,
        "model": "gemini-2.5-flash",
    }
    if event_publisher is not None:
        kwargs["event_publisher"] = event_publisher
    return TurnContext(**kwargs)  # type: ignore[arg-type]


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


def _stub_agent_md_for_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "AGENT.md"
    agent.write_text("# test agent\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_MD", str(agent))
