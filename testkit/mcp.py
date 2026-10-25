"""MCP doubles shared by tool-executor and context tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from monkeybot.core.mcp.ports_mcp import MCPCatalogApplyResult
from monkeybot.core.types.types_tools import ToolDef


class NoMCP:
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
        return []

    def catalog_names(self) -> list[str]:
        return []

    def known_server_names(self) -> list[str]:
        return []

    def is_connected(self, name: str) -> bool:
        del name
        return False

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        del prefixed_name
        return None

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        del name
        return []

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path, raise_on_error

    async def wait_until_ready(self) -> None:
        return None


class TraceNoMCP:
    """Looser ``_NoMCP`` from the subagent-trace tests. Not merged with ``NoMCP`` (D7)."""

    async def connect(self, *_a: object, **_k: object) -> list[object]:
        return []

    async def connect_streamable_http(self, *_a: object, **_k: object) -> list[object]:
        return []

    async def disconnect(self, *_a: object) -> None:
        return None

    async def call_tool(self, *_a: object, **_k: object) -> str:
        return ""

    def all_tools(self) -> list[object]:
        return []

    def catalog_names(self) -> list[object]:
        return []

    def known_server_names(self) -> list[object]:
        return []

    def is_connected(self, *_a: object) -> bool:
        return False

    def split_prefixed_tool(self, *_a: object) -> None:
        return None

    async def connect_from_catalog(self, *_a: object) -> list[object]:
        return []

    async def load_from_config(self, *_a: object, **_kw: object) -> None:
        return None

    async def wait_until_ready(self) -> None:
        return None


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

    def status(self, name: str | None = None) -> list[object]:
        del name
        return []

    async def list_resources(self, server_name: str | None = None) -> list[object]:
        del server_name
        return []

    async def read_resource(self, server_name: str, uri: str) -> dict[str, object]:
        del server_name, uri
        return {"server": "", "uri": "", "text": "", "contents": []}

    async def list_prompts(self, server_name: str | None = None) -> list[object]:
        del server_name
        return []

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del server_name, prompt_name, arguments
        return {"server": "", "name": "", "description": None, "messages": []}

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path, raise_on_error

    async def wait_until_ready(self) -> None:
        return None

    def set_env_overlay(self, env: Mapping[str, str] | None) -> dict[str, str] | None:
        del env
        return None

    async def apply_catalog_diff(
        self,
        mcp_json_path: Path,
        *,
        raise_on_error: bool = False,
    ) -> MCPCatalogApplyResult:
        del mcp_json_path, raise_on_error
        return MCPCatalogApplyResult()
