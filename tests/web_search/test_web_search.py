"""Tests for web_search package: build_backend, WebSearchTool, and the CustomTool hook."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from monkeybot.core.context import CustomTool, build_context
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.llm.provider import ToolCall
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.web_search import build_backend
from monkeybot.web_search.protocol import SearchResult
from monkeybot.web_search.tool import WebSearchTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMCP:
    async def connect(self, name: str, command: str, args: list[str], env: dict) -> list[ToolDef]:
        return []

    async def connect_streamable_http(self, name: str, url: str, headers: Any = None) -> list[ToolDef]:
        return []

    async def disconnect(self, name: str) -> None:
        pass

    async def call_tool(self, server_name: str, tool_name: str, args: dict) -> str:
        return ""

    def all_tools(self) -> list[ToolDef]:
        return []

    def split_prefixed_tool(self, name: str) -> tuple[str, str] | None:
        return None

    async def load_from_config(self, path: Path) -> None:
        pass


def _make_executor(tmp_path: Path, extra_tools: list | None = None) -> CoreToolExecutor:
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory_path=mem,
        skills_path=skills,
        mcp=_FakeMCP(),
        extra_tools=extra_tools,
    )


def _ctx_stub() -> Any:
    from monkeybot.core.context import TurnContext
    return TurnContext(
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


# ---------------------------------------------------------------------------
# build_backend
# ---------------------------------------------------------------------------


def test_build_backend_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "none")
    assert build_backend() is None


def test_build_backend_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "unknown_engine")
    with pytest.raises(ValueError, match="Unknown WEB_SEARCH_BACKEND"):
        build_backend()


def test_build_backend_tavily_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        build_backend()


def test_build_backend_firecrawl_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "firecrawl")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FIRECRAWL_API_KEY"):
        build_backend()


def test_build_backend_duckduckgo_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARCH_BACKEND", raising=False)
    backend = build_backend()
    assert backend is not None
    assert backend.name == "duckduckgo"


def test_build_backend_tavily_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    backend = build_backend()
    assert backend is not None
    assert backend.name == "tavily"


def test_build_backend_firecrawl_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "firecrawl")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    backend = build_backend()
    assert backend is not None
    assert backend.name == "firecrawl"


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_tool_returns_results() -> None:
    fake_backend = MagicMock()
    fake_backend.name = "fake"
    fake_backend.search = AsyncMock(return_value=[
        SearchResult(title="T1", url="https://a.com", snippet="S1", score=0.9),
        SearchResult(title="T2", url="https://b.com", snippet="S2"),
    ])

    tool = WebSearchTool(fake_backend)
    assert tool.tool_def.name == "web_search"

    raw = await tool.execute({"query": "hello world", "max_results": 2})
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["query"] == "hello world"
    assert len(data["results"]) == 2
    assert data["results"][0]["title"] == "T1"
    assert data["results"][0]["score"] == 0.9
    assert "score" not in data["results"][1]

    fake_backend.search.assert_awaited_once_with("hello world", max_results=2)


@pytest.mark.asyncio
async def test_web_search_tool_empty_query() -> None:
    fake_backend = MagicMock()
    fake_backend.name = "fake"
    tool = WebSearchTool(fake_backend)
    raw = await tool.execute({"query": "  "})
    data = json.loads(raw)
    assert data["ok"] is False
    assert "query" in data["error"].lower()


@pytest.mark.asyncio
async def test_web_search_tool_backend_error() -> None:
    fake_backend = MagicMock()
    fake_backend.name = "fake"
    fake_backend.search = AsyncMock(side_effect=RuntimeError("network error"))
    tool = WebSearchTool(fake_backend)

    executor = _make_executor(Path("/tmp"), extra_tools=[tool])
    ctx = _ctx_stub()
    call = ToolCall(call_id="c1", name="web_search", args={"query": "oops"})
    result, err = await executor.execute(call=call, ctx=ctx)
    assert result is None
    assert err is not None
    assert "network error" in err


# ---------------------------------------------------------------------------
# CustomTool protocol and executor dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_tool_dispatched_by_executor(tmp_path: Path) -> None:
    """A CustomTool registered via extra_tools is called when the model names it."""

    class _EchoTool:
        tool_def = ToolDef("echo", "Echo the input.", {"type": "object", "properties": {}})

        async def execute(self, args: dict[str, object]) -> str:
            return json.dumps({"echoed": args})

    executor = _make_executor(tmp_path, extra_tools=[_EchoTool()])
    ctx = _ctx_stub()
    call = ToolCall(call_id="c1", name="echo", args={"msg": "hi"})
    result, err = await executor.execute(call=call, ctx=ctx)
    assert err is None
    assert result is not None
    data = json.loads(result)
    assert data["echoed"] == {"msg": "hi"}


@pytest.mark.asyncio
async def test_custom_tool_protocol_check() -> None:
    """CustomTool is a runtime-checkable Protocol."""

    class _Good:
        tool_def = ToolDef("x", "desc", {})

        async def execute(self, args: dict[str, object]) -> str:
            return ""

    assert isinstance(_Good(), CustomTool)


# ---------------------------------------------------------------------------
# build_context wires extra_tools into TurnContext.tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_context_includes_extra_tool_defs(tmp_path: Path) -> None:
    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("ok\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()

    class _MyTool:
        tool_def = ToolDef("custom_lookup", "Look something up.", {})

        async def execute(self, args: dict[str, object]) -> str:
            return ""

    ctx = await build_context(
        "t",
        "r",
        agent_md_path=agent_path,
        memory_path=mem,
        skills_path=skills,
        mcp_client=_FakeMCP(),
        extra_tools=[_MyTool()],
    )
    names = [t.name for t in ctx.tools]
    assert "custom_lookup" in names


@pytest.mark.asyncio
async def test_build_context_no_extra_tools_unchanged(tmp_path: Path) -> None:
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
        memory_path=mem,
        skills_path=skills,
        mcp_client=_FakeMCP(),
    )
    names = [t.name for t in ctx.tools]
    assert "web_search" not in names
