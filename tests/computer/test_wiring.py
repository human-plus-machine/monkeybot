"""End-to-end registration test: computer tools reach both ``TurnContext.tools``
(the schema advertised to the model) and ``CoreToolExecutor`` (dispatch) — the
two places ``gateway/sse/app.py`` must register ``extra_tools`` for a tool to
actually be usable. Mirrors ``tests/web_search/test_web_search.py``'s pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.computer import build_computer_tools
from monkeybot.computer import safety as computer_safety
from monkeybot.core.context import build_context
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import unwrap_tool_execution_result
from tests.core.memory.helpers import make_memory_subsystem


class _FakeMCP:
    async def connect(self, name, command, args, env):
        return []

    async def connect_streamable_http(self, name, url, headers=None):
        return []

    async def disconnect(self, name):
        pass

    async def call_tool(self, server_name, tool_name, args):
        return ""

    def all_tools(self):
        return []

    def catalog_names(self):
        return []

    def known_server_names(self):
        return []

    def is_connected(self, name):
        return False

    def split_prefixed_tool(self, name):
        return None

    async def connect_from_catalog(self, name):
        return []

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        pass


def _mem_sub(root: Path) -> MemorySubsystem:
    return make_memory_subsystem(root)


@pytest.mark.asyncio
async def test_build_context_advertises_all_computer_tool_schemas(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp_client=_FakeMCP(),
        extra_tools=build_computer_tools(),
    )
    names = {t.name for t in ctx.tools}
    from monkeybot.computer import COMPUTER_TOOL_NAMES

    assert names >= COMPUTER_TOOL_NAMES


@pytest.mark.asyncio
async def test_build_context_omits_computer_tools_by_default(tmp_path: Path) -> None:
    """No extra_tools passed -> no computer_* names, matching every deployment
    that doesn't explicitly enable computer tools."""
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp_client=_FakeMCP(),
    )
    names = {t.name for t in ctx.tools}
    assert not any(n.startswith("computer_") for n in names)


@pytest.mark.asyncio
async def test_build_context_threads_approvals_persist(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()

    calls: list[tuple[str, str]] = []
    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp_client=_FakeMCP(),
        approvals_persist=lambda t, r: calls.append((t, r)),
    )
    assert ctx.approvals_persist is not None
    ctx.approvals_persist("computer_open", "/x")
    assert calls == [("computer_open", "/x")]


@pytest.mark.asyncio
async def test_core_tool_executor_dispatches_computer_clipboard_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(
        computer_safety,
        "run_argv",
        lambda argv, **kw: computer_safety.RunResult("clip-text", "", 0),
    )

    mem = tmp_path / "memory"
    executor = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=tmp_path / "skills",
        mcp=_FakeMCP(),
        extra_tools=build_computer_tools(),
    )
    from monkeybot.core.context import TurnContext

    ctx = TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )
    call = ToolCall(call_id="c1", name="computer_clipboard_read", args={})
    result, err = unwrap_tool_execution_result(await executor.execute(call=call, ctx=ctx))
    assert err is None
    data = json.loads(result)
    assert data == {"ok": True, "text": "clip-text", "truncated": False}
