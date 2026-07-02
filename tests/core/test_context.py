"""Tests for TurnContext assembly and MCP tool merging."""

import logging
from pathlib import Path

import pytest

from monkeybot.core.context import build_context, refresh_memory_index
from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.core.workspace import create_workspace_storage


def _memory_subsystem(mem_root: Path) -> MemorySubsystem:
    uri = "local://" + str(mem_root.resolve())
    fake = ScriptedFakeProvider(
        [TextDelta(text="x"), UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0), Done()]
    )
    return MemorySubsystem(
        storage=create_workspace_storage(uri),
        provider=fake,
        model="gemini-2.5-flash",
        memory_uri=uri,
    )


class FakeMCPClient:
    """Test double implementing :class:`~monkeybot.core.mcp.ports_mcp.MCPClientPort`."""

    def __init__(self, tools: list[ToolDef]) -> None:
        self._tools = tools

    async def connect(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> list[ToolDef]:
        del name, command, args, env
        return []

    async def connect_streamable_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[ToolDef]:
        del name, url, headers
        return []

    async def disconnect(self, name: str) -> None:
        del name

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, object],
    ) -> str:
        del server_name, tool_name, args
        return ""

    def all_tools(self) -> list[ToolDef]:
        return list(self._tools)

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        del prefixed_name
        return None

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path


@pytest.mark.asyncio
async def test_build_context_merges_core_and_mcp_tools(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("You are a helpful assistant.\n", encoding="utf-8")

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("alpha summary\nbeta summary\n", encoding="utf-8")

    skills = tmp_path / "skills"
    research = skills / "research"
    research.mkdir(parents=True)
    (research / "SKILL.md").write_text("Do research tasks.\n\nMore body.\n", encoding="utf-8")

    mcp_tools = [
        ToolDef("db__query", "Query database.", {}),
        ToolDef("wiki__search", "Search wiki.", {}),
    ]
    ctx = await build_context(
        "thread-1",
        "req-1",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient(mcp_tools),
    )

    assert ctx.agent_md == "You are a helpful assistant."
    assert ctx.memory_index == ["alpha summary", "beta summary"]
    assert len(ctx.skills) == 1
    assert ctx.skills[0].name == "research"
    assert ctx.skills[0].description == "Do research tasks."

    names = [t.name for t in ctx.tools]
    core_names = {
        "run_command",
        "read_file",
        "write_file",
        "replace_in_file",
        "glob",
        "search_memory",
        "list_skills",
        "task",
        "add_mcp_server",
        "remove_mcp_server",
        "start_loop",
        "loop_status",
        "pause_loop",
        "resume_loop",
        "stop_loop",
        "render_image",
        "read_attachment",
    }
    assert core_names.issubset(set(names))
    assert "db__query" in names
    assert "wiki__search" in names
    assert len(ctx.tools) == 17 + 2
    for t in ctx.tools:
        assert t.description.strip()


@pytest.mark.asyncio
async def test_build_context_omits_attachment_tools_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTACHMENTS_ENABLED", "false")
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
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )

    names = {t.name for t in ctx.tools}
    assert "render_image" not in names
    assert "read_attachment" not in names


@pytest.mark.asyncio
async def test_build_context_include_task_tool_false(tmp_path: Path) -> None:
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
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
        include_task_tool=False,
    )
    assert "task" not in [t.name for t in ctx.tools]


@pytest.mark.asyncio
async def test_build_context_missing_index_yields_empty_memory(tmp_path: Path) -> None:
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
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    assert ctx.memory_index == []


@pytest.mark.asyncio
async def test_build_context_empty_agent_md_raises(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("   \n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()

    with pytest.raises(ValueError, match=str(agent_path)):
        await build_context(
            "t",
            "r",
            agent_md_path=agent_path,
            memory=_memory_subsystem(mem),
            skills_path=skills,
            mcp_client=FakeMCPClient([]),
        )


@pytest.mark.asyncio
async def test_build_context_discovers_doc_only_skill_without_runner(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    orphan = skills / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("Only docs.\n", encoding="utf-8")

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    assert len(ctx.skills) == 1
    assert ctx.skills[0].name == "orphan"


@pytest.mark.asyncio
async def test_build_context_sets_memory_subsystem(tmp_path: Path) -> None:
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
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    assert ctx.memory is not None
    assert "local://" in ctx.memory.uri


@pytest.mark.asyncio
async def test_refresh_memory_index_no_path_returns_same_ctx(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=None,
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    out = await refresh_memory_index(ctx)
    assert out is ctx


@pytest.mark.asyncio
async def test_refresh_memory_index_picks_up_new_entries(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("first line\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    assert ctx.memory_index == ["first line"]

    (mem / "INDEX.md").write_text("first line\nsecond line\n", encoding="utf-8")
    refreshed = await refresh_memory_index(ctx)
    assert refreshed.memory_index == ["first line", "second line"]


@pytest.mark.asyncio
async def test_refresh_memory_index_silent_fail_on_unicode_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("valid line\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    (mem / "INDEX.md").write_bytes(b"\xff\xfe")

    with caplog.at_level(logging.WARNING, logger="monkeybot.core.context"):
        out = await refresh_memory_index(ctx)

    assert out is ctx
    assert ctx.memory_index == ["valid line"]
    assert any("[MEMORY]" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_refresh_memory_index_silent_fail_on_os_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("stable\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )

    async def boom(storage):
        raise OSError("simulated read failure")

    monkeypatch.setattr("monkeybot.core.memory.subsystem.async_load_index", boom)

    with caplog.at_level(logging.WARNING, logger="monkeybot.core.context"):
        out = await refresh_memory_index(ctx)

    assert out is ctx
    assert ctx.memory_index == ["stable"]
    assert any("[MEMORY]" in r.message for r in caplog.records)
