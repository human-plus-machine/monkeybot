"""Tests for session-scoped todo_list store, tool, and prompt injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext, build_context
from monkeybot.core.prompts.prompt import (
    TODO_LIST_HEADING,
    compose_system_prompt,
    compose_volatile_tail_parts,
)
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.todo_list import (
    TodoListStore,
    TodoListTool,
    todo_list_enabled_from_env,
    todo_list_mirror_to_disk_from_env,
)


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


def test_todo_list_mirror_to_disk_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONKEYBOT_TODO_LIST_MIRROR_TO_DISK", raising=False)
    assert todo_list_mirror_to_disk_from_env() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
def test_todo_list_mirror_to_disk_opt_out(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MONKEYBOT_TODO_LIST_MIRROR_TO_DISK", value)
    assert todo_list_mirror_to_disk_from_env() is False


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
async def test_todo_list_mirror_to_disk_false_skips_write(tmp_path: Path) -> None:
    store = TodoListStore("sess-no-mirror", workspace_root=tmp_path, mirror_to_disk=False)
    tool = TodoListTool(store)

    added = json.loads(await tool.execute({"action": "add", "text": "Keep in memory"}))
    assert added["ok"] is True
    assert added["item"]["text"] == "Keep in memory"
    assert "mirror_warning" not in added
    mirrors = list((tmp_path / ".monkeybot" / "transcripts").rglob("todos.json"))
    assert mirrors == []


@pytest.mark.asyncio
async def test_todo_list_store_records_mirror_error_on_disk_failure(tmp_path: Path) -> None:
    """Memory stays authoritative when the debug mirror write fails; the failure
    is recorded on ``mirror_error`` instead of raising or being fully silent."""
    store = TodoListStore("sess-mirror-fail", workspace_root=tmp_path)
    # Point the mirror at a path that cannot be created (parent is a file, not a dir).
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    store._session_dir = blocker / "session"

    item = await store.add("Ship it")
    assert not isinstance(item, str)
    assert store.mirror_error is not None
    # In-memory state is unaffected by the mirror failure.
    assert store.items[0].text == "Ship it"


@pytest.mark.asyncio
async def test_todo_list_tool_surfaces_mirror_warning_on_disk_failure(tmp_path: Path) -> None:
    store = TodoListStore("sess-mirror-fail-2", workspace_root=tmp_path)
    blocker = tmp_path / "blocked2"
    blocker.write_text("not a directory", encoding="utf-8")
    store._session_dir = blocker / "session"
    tool = TodoListTool(store)

    added = json.loads(await tool.execute({"action": "add", "text": "Ship it"}))
    assert added["ok"] is True
    assert added["item"]["text"] == "Ship it"
    assert "mirror_warning" in added


@pytest.mark.asyncio
async def test_todo_list_tool_validation_errors(tmp_path: Path) -> None:
    tool = TodoListTool(TodoListStore("s", workspace_root=tmp_path, mirror_to_disk=False))
    empty = json.loads(await tool.execute({"action": "add", "text": "  "}))
    assert empty["ok"] is False
    empty_list = json.loads(await tool.execute({"action": "add", "text": []}))
    assert empty_list["ok"] is False
    missing = json.loads(await tool.execute({"action": "complete", "id": "nope"}))
    assert missing["ok"] is False
    bad = json.loads(await tool.execute({"action": "list"}))
    assert bad["ok"] is False


@pytest.mark.asyncio
async def test_todo_list_tool_add_many_atomic(tmp_path: Path) -> None:
    store = TodoListStore("sess-many", workspace_root=tmp_path, mirror_to_disk=False)
    tool = TodoListTool(store)

    added = json.loads(
        await tool.execute(
            {
                "action": "add",
                "text": [
                    "Review PR 473",
                    "Review PR 554",
                    "Review PR 591",
                ],
            }
        )
    )
    assert added["ok"] is True
    assert "item" not in added  # bulk: use added + items, not a misleading first item
    assert [item["text"] for item in added["added"]] == [
        "Review PR 473",
        "Review PR 554",
        "Review PR 591",
    ]
    assert [item["id"] for item in added["items"]] == ["t1", "t2", "t3"]
    assert len(store.items) == 3

    # Capacity checked against the whole batch before any mutation.
    store_full = TodoListStore("sess-cap", workspace_root=tmp_path, mirror_to_disk=False)
    for i in range(49):
        item = await store_full.add(f"keep-{i}")
        assert not isinstance(item, str)
    tool_full = TodoListTool(store_full)
    over = json.loads(
        await tool_full.execute({"action": "add", "text": ["one-more", "two-more"]})
    )
    assert over["ok"] is False
    assert "would exceed max" in over["message"]
    assert len(store_full.items) == 49


@pytest.mark.asyncio
async def test_todo_list_add_many_rejects_blank_without_partial_write(tmp_path: Path) -> None:
    store = TodoListStore("sess-blank", workspace_root=tmp_path, mirror_to_disk=False)
    await store.add("already there")
    tool = TodoListTool(store)

    bad = json.loads(await tool.execute({"action": "add", "text": ["ok", "  ", "also"]}))
    assert bad["ok"] is False
    assert [item.text for item in store.items] == ["already there"]


@pytest.mark.asyncio
async def test_volatile_tail_includes_todo_list_when_non_empty(tmp_path: Path) -> None:
    store = TodoListStore("s", workspace_root=tmp_path, mirror_to_disk=False)
    await store.add("First")
    await store.add("Second")
    await store.complete("t1")
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
    assert "## Todo list" in full


def test_volatile_tail_omits_todo_list_when_empty(tmp_path: Path) -> None:
    store = TodoListStore("s", workspace_root=tmp_path, mirror_to_disk=False)
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
    store = TodoListStore("thread-1", workspace_root=tmp_path, mirror_to_disk=False)
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
