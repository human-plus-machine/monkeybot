"""Idle counting and per-session hiding for MCP servers."""

from __future__ import annotations

from monkeybot.core.mcp.session_visibility import (
    IDLE_INNER_TURNS,
    MCPSessionVisibility,
    advertised_servers,
    idle_note,
    servers_used_by_calls,
)
from monkeybot.core.types.types_tools import ToolDef


def _offered(visibility: MCPSessionVisibility, server: str) -> None:
    visibility.note_inner_turn({server}, set())


def test_idle_counts_only_offered_unused_servers() -> None:
    visibility = MCPSessionVisibility()
    visibility.note_inner_turn(set(), set())
    visibility.note_inner_turn({"browser"}, {"browser"})
    _offered(visibility, "wiki")
    assert visibility.idle_count("browser") == 0
    assert visibility.idle_count("wiki") == 1
    assert visibility.idle_count("absent") == 0


def test_use_resets_the_count() -> None:
    visibility = MCPSessionVisibility()
    for _ in range(4):
        _offered(visibility, "browser")
    visibility.note_inner_turn({"browser"}, {"browser"})
    assert visibility.idle_count("browser") == 0


def test_tenth_idle_turn_is_returned_once() -> None:
    visibility = MCPSessionVisibility()
    for _ in range(IDLE_INNER_TURNS - 1):
        _offered(visibility, "browser")
    assert visibility.take_newly_idle() == []
    _offered(visibility, "browser")
    assert visibility.take_newly_idle() == ["browser"]
    assert visibility.is_hidden("browser")
    assert visibility.idle_count("browser") == 0
    _offered(visibility, "browser")
    assert visibility.take_newly_idle() == []


def test_record_use_unhides() -> None:
    visibility = MCPSessionVisibility()
    for _ in range(IDLE_INNER_TURNS):
        _offered(visibility, "browser")
    visibility.take_newly_idle()
    visibility.record_use("browser")
    assert not visibility.is_hidden("browser")
    assert visibility.idle_count("browser") == 0


def test_remembered_call_does_not_unhide() -> None:
    visibility = MCPSessionVisibility()
    visibility.hidden.add("browser")
    visibility.note_inner_turn(set(), {"browser"})
    assert visibility.is_hidden("browser")


def test_filter_tools_keeps_other_servers_and_core_tools() -> None:
    visibility = MCPSessionVisibility()
    visibility.hidden.add("browser")
    tools = [
        ToolDef("list_dir", "List", {}),
        ToolDef("browser__goto", "Go", {}),
        ToolDef("wiki__search", "Search", {}),
    ]
    kept = [tool.name for tool in visibility.filter_tools(tools, ["browser", "wiki"])]
    assert kept == ["list_dir", "wiki__search"]


def test_idle_note_names_the_server_and_the_limit() -> None:
    text = idle_note(["browser"])
    assert "MCP server `browser`" in text
    assert f"after {IDLE_INNER_TURNS} turns without use" in text
    assert "Call `enable_mcp` with name `browser`" in text


def test_advertised_and_used_prefer_the_longest_server_name() -> None:
    tools = [ToolDef("fix__extra__echo", "Echo", {}), ToolDef("fix__echo", "Echo", {})]
    known = ["fix", "fix__extra"]
    assert advertised_servers(tools, known) == {"fix", "fix__extra"}
    used = servers_used_by_calls([("fix__extra__echo", {"text": "x"})], known)
    assert used == {"fix__extra"}
    listed = servers_used_by_calls([("list_mcp_resources", {"server": "fix"})], known)
    assert listed == {"fix"}
    enabled = servers_used_by_calls([("enable_mcp", {"name": "fix"})], known)
    assert enabled == {"fix"}
