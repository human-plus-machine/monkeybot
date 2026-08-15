"""Tests for TurnContext assembly and MCP tool merging."""

import logging
from pathlib import Path

import pytest

from monkeybot.core.context import (
    SCHEDULED_LOOP_TOOL_NAMES,
    _discover_skills,
    _parse_skill_description,
    build_context,
    refresh_memory_index,
    refresh_tools_after_loops_change,
    refresh_tools_after_mcp_change,
)
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.types.types_tools import ToolDef
from tests.core.memory.helpers import make_memory_subsystem


def _memory_subsystem(mem_root: Path) -> MemorySubsystem:
    return make_memory_subsystem(mem_root)


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

    def catalog_names(self) -> list[str]:
        return []

    def known_server_names(self) -> list[str]:
        names: set[str] = set()
        for tool in self._tools:
            if "__" in tool.name:
                names.add(tool.name.split("__", 1)[0])
        return sorted(names)

    def is_connected(self, name: str) -> bool:
        return name in self.known_server_names()

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        del prefixed_name
        return None

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        del name
        return []

    def status(self, name: str | None = None):
        del name
        return []

    async def list_resources(self, server_name: str | None = None):
        del server_name
        return []

    async def read_resource(self, server_name: str, uri: str):
        del server_name, uri
        return {"server": "", "uri": "", "text": "", "contents": []}

    async def list_prompts(self, server_name: str | None = None):
        del server_name
        return []

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ):
        del server_name, prompt_name, arguments
        return {"server": "", "name": "", "description": None, "messages": []}

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path, raise_on_error


# Standard trusted skills fixture (e.g. evals/smoke_agent) — regression for list_skills descriptions.
_IMAGE_GENERATOR_SKILL_MD = """---
name: image-generator
description: Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat.
---

# image-generator

Generate images using **Vertex AI** Gemini image models.
"""

_BROWSER_SKILL_MD = """---
name: browser
description: Control a real browser via CDP for web tasks; check and write site playbooks before improvising.
---

# browser

Use the **browser** MCP tools for web interaction.
"""


def _write_skill(skills_root: Path, folder: str, content: str) -> None:
    skill_dir = skills_root / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("skill_md", "folder", "expected"),
    [
        (
            _IMAGE_GENERATOR_SKILL_MD,
            "image-generator",
            "Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat.",
        ),
        (
            _BROWSER_SKILL_MD,
            "browser",
            "Control a real browser via CDP for web tasks; check and write site playbooks before improvising.",
        ),
        ("Do research tasks.\n\nMore body.\n", "research", "Do research tasks."),
        (
            "---\nname: bare\n---\n\n# bare\n\nBody only.\n",
            "bare",
            "bare",
        ),
    ],
)
def test_parse_skill_description(skill_md: str, folder: str, expected: str) -> None:
    assert _parse_skill_description(skill_md, folder) == expected


def test_parse_skill_description_frontmatter_not_markdown_heading() -> None:
    """Regression: YAML description must win over ``# skill-name`` body heading."""
    desc = _parse_skill_description(_IMAGE_GENERATOR_SKILL_MD, "image-generator")
    assert desc != "# image-generator"
    assert desc != "image-generator"
    assert "Vertex AI" in desc


def test_discover_skills_returns_frontmatter_descriptions(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "image-generator", _IMAGE_GENERATOR_SKILL_MD)
    _write_skill(skills, "browser", _BROWSER_SKILL_MD)

    discovered = _discover_skills(skills)

    assert [(s.name, s.description) for s in discovered] == [
        (
            "browser",
            "Control a real browser via CDP for web tasks; check and write site playbooks before improvising.",
        ),
        (
            "image-generator",
            "Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat.",
        ),
    ]


@pytest.mark.asyncio
async def test_build_context_merges_core_and_mcp_tools(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("You are a helpful assistant.\n", encoding="utf-8")

    mem = tmp_path / "memory"
    mem.mkdir()

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
    assert any("IDENTITY" in line or "No memories yet" in line for line in ctx.memory_index)
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
        "grep",
        "apply_patch",
        "search",
        "list_skills",
        "task",
        "enable_mcp",
        "disable_mcp",
        "enable_loops",
        "list_mcp_resources",
        "read_mcp_resource",
        "list_mcp_prompts",
        "get_mcp_prompt",
        "load_file",
    }
    assert core_names.issubset(set(names))
    assert "start_loop" not in names
    assert "loop_status" not in names
    assert "pause_loop" not in names
    assert "resume_loop" not in names
    assert "stop_loop" not in names
    assert "disable_loops" not in names
    assert "db__query" in names
    assert "wiki__search" in names
    assert "list_mcp_resources" in names
    assert "read_mcp_resource" in names
    assert "list_mcp_prompts" in names
    assert "get_mcp_prompt" in names
    assert len(ctx.tools) == len(core_names) + 2
    for t in ctx.tools:
        assert t.description.strip()


@pytest.mark.asyncio
async def test_build_context_omits_progressive_mcp_meta_without_connected_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTACHMENTS_ENABLED", "true")
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("You are a helpful assistant.\n", encoding="utf-8")
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ctx = await build_context(
        "thread-1",
        "req-1",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    names = {t.name for t in ctx.tools}
    assert "list_mcp_resources" not in names
    assert "read_mcp_resource" not in names
    assert "list_mcp_prompts" not in names
    assert "get_mcp_prompt" not in names
    assert "enable_mcp" in names
    assert "enable_loops" in names
    assert "disable_loops" not in names
    assert "start_loop" not in names


def test_refresh_tools_after_mcp_change_adds_and_drops_progressive_meta_tools() -> None:
    class _ToggleMCP(FakeMCPClient):
        def __init__(self) -> None:
            super().__init__([])
            self._connected: dict[str, list[ToolDef]] = {}

        def known_server_names(self) -> list[str]:
            return ["browser"]

        def is_connected(self, name: str) -> bool:
            return name in self._connected

        def all_tools(self) -> list[ToolDef]:
            out: list[ToolDef] = []
            for tools in self._connected.values():
                out.extend(tools)
            return out

        def connect_browser(self) -> None:
            self._connected["browser"] = [ToolDef("browser__goto", "Go", {})]

        def disconnect_browser(self) -> None:
            self._connected.pop("browser", None)

    from monkeybot.core.context import TurnContext

    mcp = _ToggleMCP()
    ctx = TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("enable_mcp", "Enable", {}),
            ToolDef("read_file", "Read", {}),
        ],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )

    mcp.connect_browser()
    refresh_tools_after_mcp_change(ctx, mcp)
    names = [t.name for t in ctx.tools]
    assert "browser__goto" in names
    assert "list_mcp_resources" in names
    assert "read_mcp_resource" in names
    assert "list_mcp_prompts" in names
    assert "get_mcp_prompt" in names
    assert names.count("list_mcp_resources") == 1
    assert names.count("list_mcp_prompts") == 1

    mcp.disconnect_browser()
    refresh_tools_after_mcp_change(ctx, mcp)
    names = [t.name for t in ctx.tools]
    assert "browser__goto" not in names
    assert "list_mcp_resources" not in names
    assert "read_mcp_resource" not in names
    assert "list_mcp_prompts" not in names
    assert "get_mcp_prompt" not in names
    assert "enable_mcp" in names
    assert "read_file" in names


def test_refresh_tools_after_loops_change_adds_and_drops_lifecycle_tools() -> None:
    from monkeybot.core.context import TurnContext

    ctx = TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[
            ToolDef("enable_loops", "Enable", {}),
            ToolDef("read_file", "Read", {}),
        ],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )

    refresh_tools_after_loops_change(ctx, loops_advertised=True)
    names = {t.name for t in ctx.tools}
    assert SCHEDULED_LOOP_TOOL_NAMES.issubset(names)
    assert "disable_loops" in names
    assert "enable_loops" in names
    assert "read_file" in names

    refresh_tools_after_loops_change(ctx, loops_advertised=False)
    names = {t.name for t in ctx.tools}
    assert names.isdisjoint(SCHEDULED_LOOP_TOOL_NAMES)
    assert "disable_loops" not in names
    assert "enable_loops" in names
    assert "read_file" in names


@pytest.mark.asyncio
async def test_build_context_advertises_loop_tools_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTACHMENTS_ENABLED", "false")
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("You are a helpful assistant.\n", encoding="utf-8")
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ctx = await build_context(
        "thread-1",
        "req-1",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
        scheduled_loops_available=True,
        loops_advertised=True,
    )
    names = {t.name for t in ctx.tools}
    assert SCHEDULED_LOOP_TOOL_NAMES.issubset(names)
    assert ctx.scheduled_loops_available is True


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
    assert "load_file" not in names


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
    assert any("IDENTITY" in line or "No memories yet" in line for line in ctx.memory_index)


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
async def test_build_context_discovers_skill_description_from_frontmatter(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    _write_skill(skills, "image-generator", _IMAGE_GENERATOR_SKILL_MD)

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=_memory_subsystem(mem),
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )

    assert len(ctx.skills) == 1
    assert ctx.skills[0].name == "image-generator"
    assert ctx.skills[0].description == (
        "Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat."
    )
    assert ctx.skills[0].description != "# image-generator"


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
    skills = tmp_path / "skills"
    skills.mkdir()
    memory = _memory_subsystem(mem)
    memory._palace.upsert_drawer(
        "d1",
        "first line",
        {"wing": "main", "room": "conversation", "filed_at": "2026-01-01T00:00:00Z"},
    )

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory=memory,
        skills_path=skills,
        mcp_client=FakeMCPClient([]),
    )
    assert any("first line" in line for line in ctx.memory_index)

    memory._palace.upsert_drawer(
        "d2",
        "second line",
        {"wing": "main", "room": "conversation", "filed_at": "2026-01-02T00:00:00Z"},
    )
    refreshed = await refresh_memory_index(ctx)
    assert any("second line" in line for line in refreshed.memory_index)


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
    original = list(ctx.memory_index)

    async def boom() -> list[str]:
        raise OSError("simulated read failure")

    monkeypatch.setattr(ctx.memory, "load_index", boom)

    with caplog.at_level(logging.WARNING, logger="monkeybot.core.context"):
        out = await refresh_memory_index(ctx)

    assert out is ctx
    assert ctx.memory_index == original
    assert any("[MEMORY]" in r.message for r in caplog.records)
