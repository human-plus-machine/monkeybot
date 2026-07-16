"""Tests for session-scoped todo_list store, tool, and prompt injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext, build_context
from monkeybot.core.prompts.harness_prompt import harness_fixed_context
from monkeybot.core.prompts.prompt import (
    TODO_LIST_HEADING,
    compose_system_prompt,
    compose_volatile_tail_parts,
)
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.todo_list import TodoListStore, TodoListTool, todo_list_enabled_from_env


class _FakeMCP:
    def all_tools(self) -> list[ToolDef]:
        return []

    def catalog_names(self) -> list[str]:
        return []

    def known_server_names(self) -> list[str]:
        return []

    def is_connected(self, name: str) -> bool:
        return False

    def split_prefixed_tool(self, name: str) -> tuple[str, str] | None:
        return None

    async def connect(self, name: str, command: str, args: list[str], env: dict) -> list[ToolDef]:
        return []

    async def connect_streamable_http(self, name: str, url: str, headers=None) -> list[ToolDef]:
        return []

    async def disconnect(self, name: str) -> None:
        pass

    async def call_tool(self, server_name: str, tool_name: str, args: dict) -> str:
        return ""

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        return []

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        pass


def test_todo_list_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_TODO_LIST_ENABLED", raising=False)
    assert todo_list_enabled_from_env() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_todo_list_enabled_opt_out(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MONKEYBOT_TODO_LIST_ENABLED", value)
    assert todo_list_enabled_from_env() is False


@pytest.mark.asyncio
async def test_todo_list_tool_add_complete_remove_and_disk_mirror(tmp_path: Path) -> None:
    store = TodoListStore("sess-1", workspace_root=tmp_path)
    tool = TodoListTool(store)

    added = json.loads(await tool.execute({"action": "add", "text": "Wire gateway"}))
    assert added["ok"] is True
    assert added["item"]["id"] == "t1"
    assert added["item"]["status"] == "pending"
    assert len(added["items"]) == 1

    mirror = list((tmp_path / ".monkeybot" / "transcripts").rglob("todos.json"))
    assert len(mirror) == 1
    disk = json.loads(mirror[0].read_text(encoding="utf-8"))
    assert disk["session_id"] == "sess-1"
    assert disk["items"][0]["text"] == "Wire gateway"

    done = json.loads(await tool.execute({"action": "complete", "id": "t1"}))
    assert done["ok"] is True
    assert done["item"]["status"] == "done"
    assert store.items[0].status == "done"

    removed = json.loads(await tool.execute({"action": "remove", "id": "t1"}))
    assert removed["ok"] is True
    assert removed["items"] == []
    assert store.items == ()
    disk_after = json.loads(mirror[0].read_text(encoding="utf-8"))
    assert disk_after["items"] == []


@pytest.mark.asyncio
async def test_todo_list_tool_validation_errors(tmp_path: Path) -> None:
    tool = TodoListTool(TodoListStore("s", workspace_root=tmp_path))
    empty = json.loads(await tool.execute({"action": "add", "text": "  "}))
    assert empty["ok"] is False
    missing = json.loads(await tool.execute({"action": "complete", "id": "nope"}))
    assert missing["ok"] is False
    bad = json.loads(await tool.execute({"action": "list"}))
    assert bad["ok"] is False


def test_harness_includes_todo_list_when_enabled() -> None:
    out = harness_fixed_context(include_task_tool=False, include_todo_list=True)
    assert "`todo_list`" in out
    assert "`add` / `complete` / `remove`" in out
    assert "## Todo list" in out


def test_harness_omits_todo_list_when_disabled() -> None:
    out = harness_fixed_context(include_task_tool=False, include_todo_list=False)
    assert "`todo_list`" not in out


def test_volatile_tail_includes_todo_list_when_non_empty(tmp_path: Path) -> None:
    store = TodoListStore("s", workspace_root=tmp_path)
    store.add("First")
    store.add("Second")
    store.complete("t1")
    ctx = TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[ToolDef("todo_list", "todos", {"type": "object", "properties": {}})],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        todo_store=store,
    )
    parts = compose_volatile_tail_parts(ctx)
    assert TODO_LIST_HEADING in parts["todos"]
    assert "1. [done] First" in parts["todos"]
    assert "2. [pending] Second" in parts["todos"]
    full = compose_system_prompt(ctx)
    assert "`todo_list`" in full
    assert "## Todo list" in full


def test_volatile_tail_omits_todo_list_when_empty(tmp_path: Path) -> None:
    store = TodoListStore("s", workspace_root=tmp_path)
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
        todo_store=store,
    )
    assert compose_volatile_tail_parts(ctx)["todos"] == ""


@pytest.mark.asyncio
async def test_build_context_and_executor_dispatch_todo_list(tmp_path: Path) -> None:
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Agent\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    store = TodoListStore("thread-1", workspace_root=tmp_path)
    tool = TodoListTool(store)
    ctx = await build_context(
        "thread-1",
        "req-1",
        agent_md_path=agent,
        memory=None,
        skills_path=skills,
        mcp_client=_FakeMCP(),
        model="gemini-2.5-flash",
        workspace_root=tmp_path,
        include_task_tool=False,
        extra_tools=[tool],
        todo_store=store,
    )
    assert any(t.name == "todo_list" for t in ctx.tools)
    assert ctx.todo_store is store

    executor = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_FakeMCP(),
        extra_tools=[tool],
    )
    from monkeybot.core.llm.provider import ToolCall
    from monkeybot.core.tools.types import unwrap_tool_execution_result

    result = await executor.execute(
        call=ToolCall(name="todo_list", call_id="c1", args={"action": "add", "text": "Ship it"}),
        ctx=ctx,
    )
    text, err = unwrap_tool_execution_result(result)
    assert err is None
    assert text is not None
    payload = json.loads(text)
    assert payload["ok"] is True
    assert payload["item"]["text"] == "Ship it"
